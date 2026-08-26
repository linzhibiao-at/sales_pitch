#!/usr/bin/env python3
"""从 SKU 数据中按 role 提取所有 category_l2，生成跨 role 的笛卡尔积中类组合，
调用大模型对每个组合进行搭配合理性打分，最终输出 YAML 配置文件。

用法示例：
  # 1. 先预览组合数量（不调用 LLM）
  python scripts/gen_category_l2_cartesian_pairing.py --dry-run

  # 2. 执行打分并生成 YAML
  python scripts/gen_category_l2_cartesian_pairing.py

  # 3. 指定 role 对（仅 top x bottoms）
  python scripts/gen_category_l2_cartesian_pairing.py --role-pairs top:bottoms

  # 4. 调整批大小和并发
  python scripts/gen_category_l2_cartesian_pairing.py --batch-size 30 --max-workers 3

  # 5. 过滤掉 SKU 数量 < 5 的小众中类
  python scripts/gen_category_l2_cartesian_pairing.py --min-sku-count 5
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from itertools import combinations, product as cartesian_product
from pathlib import Path
from typing import Any

import yaml

_SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = _SCRIPT_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import env_or_empty, load_config
from scripts._project_paths import load_paths

_PATHS = load_paths()
SKUS_JSONL = _PATHS["processed_dir"] / "skus.jsonl"
DEFAULT_OUTPUT = (
    ROOT / "backend" / "intent" / "dictionaries" / "category_l2_cartesian_pairing.yaml"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

ROLE_CN = {
    "top": "上装",
    "bottoms": "下装",
    "shoes": "鞋",
    "accessory": "配饰",
    "dress": "连衣裙",
}
CN_TO_ROLE = {v: k for k, v in ROLE_CN.items()}

ALL_ROLE_PAIRS = list(combinations(sorted(ROLE_CN.keys()), 2))

MAX_RETRIES = 3
RETRY_DELAY_SEC = 2.0

# 不参与搭配的品类（内衣等非外穿品类）
CATEGORY_BLACKLIST = {"内裤", "平角裤"}

# 加载 non_clothing_exclusion.yaml 中的排除中类
_EXCL_PATH = ROOT / "backend" / "intent" / "dictionaries" / "non_clothing_exclusion.yaml"
if _EXCL_PATH.is_file():
    with _EXCL_PATH.open(encoding="utf-8") as _f:
        _excl_data = yaml.safe_load(_f) or {}
    CATEGORY_BLACKLIST |= set(_excl_data.get("non_clothing", []))
    CATEGORY_BLACKLIST |= set(_excl_data.get("intimate_swimwear", []))

# 儿童品类前缀，用于阻止儿童×成人跨人群搭配
CHILDREN_PREFIX = "儿童"

# ── Prompt ───────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "你是 FILA 品牌的专业穿搭顾问。"
    "FILA 是意大利运动时尚品牌，产品定位运动、休闲、户外。"
    "你需要对服饰中类搭配组合进行合理性打分，用于搭配推荐系统。"
    "打分应严格，只有真正常见且合理的搭配才能获得高分。"
)

BATCH_PROMPT_TEMPLATE = """请对以下 {n} 组 FILA 品牌服饰中类搭配组合进行打分。

## 背景说明

这是一个搭配推荐系统：当用户选择了某个品类的商品后，系统根据打分推荐可搭配的其他品类商品。
因此这里的"搭配"是指**一套完整穿搭中不同部件的组合**，不是叠穿。

每组是两个不同角色（上装、下装、鞋、配饰、连衣裙）的中类组合。

## 评分维度

1. **品类搭配合理性**：这两个中类作为一套穿搭的不同部件，是否合理？
2. **角色互斥性**（重要）：
   - 连衣裙是完整着装（覆盖上下身），详见下方"硬性扣分规则 2"
   - 上装+下装是最基础的搭配组合，通常得分较高
3. **季节一致性**（重要）：冬季单品不应与夏季单品搭配，详见下方"硬性扣分规则 1"
4. **风格一致性**：FILA 是运动休闲品牌，评分以运动、休闲、户外场景为主。纯商务、正装、法式浪漫等非品牌风格不加分
5. **场景适配性**：在 FILA 目标场景（运动、日常休闲、户外出行、校园）中的适用性
6. **受众接受度**：消费者实际购买和穿着这种组合的可能性

## 硬性扣分规则（必须严格执行）

以下场景存在明确冲突，**不得**通过"边缘场景"（如滑雪、登山）来合理化打高分。

### 规则 1：季节互斥
冬季单品（中长羽绒服、冲锋衣两件套、针织帽）与夏季单品（短裤、短袖、背心、凉鞋、拖鞋）搭配时，得分 **≤ 0.35**。
- 羽绒服/冲锋衣 + 短裤（梭织短裤、针织短裤）→ ≤ 0.35
- 针织帽 + 短袖类（短袖T、短袖T恤、短袖POLO、短袖梭织上衣、短袖编织衫、短袖针织上衣、短袖衬衫）→ ≤ 0.35
- 针织帽 + 背心 → ≤ 0.35
- 针织帽 + 短裤（梭织短裤、针织短裤）→ ≤ 0.35
- 不要用"滑雪""登山徒步""夏季运动休闲风"等小众场景来合理化季节冲突

### 规则 2：连衣裙 + 非外套上装互斥
连衣裙（含梭织连衣裙、针织连衣裙）是完整着装，不需要搭配上装。
- 连衣裙 + 非外套上装（如连帽卫衣、背心、毛衣、针织上衣、编织衫、短袖T、套头卫衣）→ ≤ 0.20
- 连衣裙 + 外套类（梭织外套、冲锋衣、防晒服、中长羽绒服、梭织马甲）→ 可给分，作为外搭场景，但不超过 0.50
- **注意**：此规则适用于所有连衣裙变体（梭织连衣裙、针织连衣裙、连衣裙），评分标准必须一致

### 规则 3：专业运动鞋 + 非运动裙装
专业运动鞋（FITNESS跑步鞋、路跑鞋、TENNIS性能网球鞋、GOLF软钉高球鞋、专业滑板鞋）与非运动裙装（半身裙、梭织半裙、针织半裙）风格冲突，得分 **≤ 0.40**。
- 运动裤裙（梭织裤裙、针织裤裙）因在网球/高尔夫场景中常见，可适当给分（0.5-0.7）
- 半身裙、梭织半裙、针织半裙不属于运动裙装，不应与专业运动鞋获得高分

## 评分规则

- 每组给出 0.0 到 1.0 之间的得分（保留 2 位小数）
- 0.8-1.0：经典搭配，极其常见合理（如短袖T+梭织长裤）
- 0.6-0.8：合理搭配，较常见
- 0.4-0.6：可以搭配但不太常见，有一定局限性
- 0.2-0.4：勉强可搭配，风格或功能有冲突
- 0.0-0.2：搭配不合理或角色互斥（如T恤+连衣裙、针织长裤+连衣裙）

**评分应严格区分**：预期大约 20-30% 的组合应在 0.8 以上，30-40% 在 0.4-0.8，30-40% 在 0.4 以下。请不要给大部分组合都打高分。
**优先执行硬性扣分规则**：上述硬性规则中明确限定分数上限的场景，即使其他维度看似合理，也不得突破上限。

## 待评估组合

{combinations_text}

## 输出格式

严格以 JSON 格式输出，不要输出其他内容：

```json
{{
  "scores": [
    {{"id": 1, "score": 0.85, "tags": ["经典", "百搭"], "note": "短袖T配梭织长裤是最经典的日常休闲搭配"}},
    {{"id": 2, "score": 0.45, "tags": ["小众"], "note": "背心配梭织裤裙风格差异较大"}},
    {{"id": 3, "score": 0.15, "tags": ["冲突"], "note": "连衣裙不需要搭配T恤，角色互斥"}}
  ]
}}
```

其中：
- id：组合编号（与输入对应）
- score：综合得分
- tags：搭配标签，可选值：经典、百搭、运动、休闲、户外、商务、潮流、小众、冲突
- note：一句话说明（20字以内）
"""


# ── Data loading ─────────────────────────────────────────────────────

def load_categories_by_role(
    skus_path: Path,
    min_count: int = 1,
) -> dict[str, dict[str, int]]:
    """从 skus.jsonl 读取每个 role 下的 category_l2 及其 SKU 数量。"""
    role_cats: dict[str, Counter[str]] = defaultdict(Counter)
    with skus_path.open(encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            role = (row.get("role") or "").strip()
            cat = (row.get("category_l2") or "").strip()
            if role and cat and role != "unknown":
                role_cats[role][cat] += 1

    result: dict[str, dict[str, int]] = {}
    for role in sorted(role_cats):
        filtered = {
            cat: cnt
            for cat, cnt in role_cats[role].most_common()
            if cnt >= min_count and cat not in CATEGORY_BLACKLIST
        }
        if filtered:
            result[role] = filtered
    return result


def _is_children_category(cat: str) -> bool:
    """判断品类名是否为儿童品类。"""
    return cat.startswith(CHILDREN_PREFIX)


def generate_pairwise_combos(
    cats_by_role: dict[str, dict[str, int]],
    role_pairs: list[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """生成指定 role 对之间的笛卡尔积中类组合。

    自动跳过儿童品类与成人品类的交叉组合（两端必须同为儿童或同为成人）。
    """
    pairs = role_pairs or ALL_ROLE_PAIRS
    combos: list[dict[str, Any]] = []
    idx = 0
    for r1, r2 in pairs:
        if r1 not in cats_by_role or r2 not in cats_by_role:
            continue
        cats1 = sorted(cats_by_role[r1].keys())
        cats2 = sorted(cats_by_role[r2].keys())
        for c1, c2 in cartesian_product(cats1, cats2):
            # 跳过儿童×成人交叉搭配
            if _is_children_category(c1) != _is_children_category(c2):
                continue
            idx += 1
            combos.append({
                "id": idx,
                "role_1": r1,
                "role_2": r2,
                "category_l2_1": c1,
                "category_l2_2": c2,
                "sku_count_1": cats_by_role[r1][c1],
                "sku_count_2": cats_by_role[r2][c2],
            })
    return combos


# ── LLM calling ──────────────────────────────────────────────────────

def _resolve_llm_settings(model_override: str | None = None) -> dict[str, Any]:
    cfg = load_config()
    mcfg = (cfg.get("models") or {}).get("ranking_llm") or {}
    base = (mcfg.get("base_url") or "").strip().rstrip("/")
    key_env = str(mcfg.get("api_key_env") or "ANTA_LLM_API_KEY")
    api_key = env_or_empty(key_env) or os.environ.get("OPENAI_API_KEY", "")
    model = model_override or "glm-5.2"
    enable_thinking = mcfg.get("enable_thinking")
    enable_thinking = bool(enable_thinking) if enable_thinking is not None else True
    return {
        "api_base": base,
        "api_key": api_key,
        "model": model,
        "max_tokens": int(mcfg.get("max_tokens") or 4096),
        "timeout_sec": float(mcfg.get("timeout_sec") or 180),
        "enable_thinking": enable_thinking,
    }


def _format_combos_text(batch: list[dict[str, Any]]) -> str:
    lines = []
    for item in batch:
        role1_cn = ROLE_CN.get(item["role_1"], item["role_1"])
        role2_cn = ROLE_CN.get(item["role_2"], item["role_2"])
        lines.append(
            f"{item['id']}. [{role1_cn}] {item['category_l2_1']}  +  "
            f"[{role2_cn}] {item['category_l2_2']}"
        )
    return "\n".join(lines)


def call_llm_score_batch(
    batch: list[dict[str, Any]],
    llm_settings: dict[str, Any],
) -> list[dict[str, Any]]:
    """调用 LLM 对一个批次的中类组合打分，返回 scores 列表。"""
    from openai import OpenAI

    combos_text = _format_combos_text(batch)
    user_prompt = BATCH_PROMPT_TEMPLATE.format(
        n=len(batch),
        combinations_text=combos_text,
    )

    client = OpenAI(
        api_key=llm_settings["api_key"],
        base_url=llm_settings["api_base"],
        timeout=llm_settings["timeout_sec"],
    )

    kwargs: dict[str, Any] = dict(
        model=llm_settings["model"],
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=llm_settings["max_tokens"],
        temperature=0.1,
        top_p=0.1,
    )

    extra_body: dict[str, Any] = {}
    enable_thinking = llm_settings.get("enable_thinking")
    if enable_thinking is not None:
        extra_body["enable_thinking"] = enable_thinking
        if not enable_thinking:
            extra_body["chat_template_kwargs"] = {"enable_thinking": False}
    if extra_body:
        kwargs["extra_body"] = extra_body

    for attempt in range(MAX_RETRIES):
        try:
            resp = client.chat.completions.create(**kwargs)
            raw = (resp.choices[0].message.content or "").strip()
            # 去掉 markdown 围栏
            if raw.startswith("```"):
                lines = raw.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                raw = "\n".join(lines)
            parsed = json.loads(raw)
            return parsed.get("scores") or []
        except json.JSONDecodeError:
            log.warning(
                "batch %d-%d: JSON 解析失败(attempt %d), raw=%s",
                batch[0]["id"], batch[-1]["id"], attempt + 1, raw[:200],
            )
        except Exception as e:
            log.warning(
                "batch %d-%d: LLM 调用失败(attempt %d): %s",
                batch[0]["id"], batch[-1]["id"], attempt + 1, e,
            )
        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_DELAY_SEC * (attempt + 1))

    log.error(
        "batch %d-%d: 重试 %d 次后仍失败，跳过",
        batch[0]["id"], batch[-1]["id"], MAX_RETRIES,
    )
    return []


# ── Orchestration ────────────────────────────────────────────────────

def score_all_combos(
    combos: list[dict[str, Any]],
    *,
    batch_size: int = 50,
    max_workers: int = 3,
    model_override: str | None = None,
) -> dict[int, dict[str, Any]]:
    """分批并发调用 LLM 对所有组合打分。"""
    llm_settings = _resolve_llm_settings(model_override=model_override)
    batches = [
        combos[i:i + batch_size]
        for i in range(0, len(combos), batch_size)
    ]
    log.info(
        "共 %d 组组合，分 %d 批（每批 %d），并发 %d",
        len(combos), len(batches), batch_size, max_workers,
    )

    scores_map: dict[int, dict[str, Any]] = {}
    completed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(call_llm_score_batch, batch, llm_settings): batch
            for batch in batches
        }
        for future in as_completed(futures):
            batch = futures[future]
            try:
                results = future.result()
                for item in results:
                    cid = item.get("id")
                    if cid is not None:
                        scores_map[int(cid)] = item
            except Exception as e:
                log.error(
                    "batch %d-%d 异常: %s",
                    batch[0]["id"], batch[-1]["id"], e,
                )
            completed += 1
            if completed % 10 == 0 or completed == len(batches):
                log.info("进度: %d/%d 批完成", completed, len(batches))

    return scores_map


# ── YAML output ──────────────────────────────────────────────────────

def build_output(
    cats_by_role: dict[str, dict[str, int]],
    combos: list[dict[str, Any]],
    scores_map: dict[int, dict[str, Any]],
    *,
    primary_threshold: float = 0.6,
    allowed_threshold: float = 0.4,
) -> dict[str, Any]:
    """将打分结果组装为与 category_l2_pairing.yaml 兼容的 YAML 结构。"""

    # 按 anchor category 聚合
    anchor_companions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for combo in combos:
        cid = combo["id"]
        score_info = scores_map.get(cid, {})
        score = float(score_info.get("score", 0))
        tags = score_info.get("tags", [])
        note = score_info.get("note", "")

        # 双向记录：c1 作为 anchor 时 c2 是 companion，反之亦然
        for anchor_key, comp_key, anchor_role_key, comp_role_key in [
            ("category_l2_1", "category_l2_2", "role_1", "role_2"),
            ("category_l2_2", "category_l2_1", "role_2", "role_1"),
        ]:
            anchor_companions[combo[anchor_key]].append({
                "category_l2": combo[comp_key],
                "anchor_role": combo[anchor_role_key],
                "companion_role": combo[comp_role_key],
                "score": round(score, 2),
                "tags": tags,
                "note": note,
            })

    # 构建 pairing_rules
    pairing_rules: dict[str, Any] = {}
    all_anchors = set()
    for role, cats in cats_by_role.items():
        all_anchors.update(cats.keys())

    for anchor in sorted(all_anchors):
        companions_raw = anchor_companions.get(anchor, [])
        # 去重：同一 companion 取最高分
        best: dict[str, dict[str, Any]] = {}
        for c in companions_raw:
            key = c["category_l2"]
            if key not in best or c["score"] > best[key]["score"]:
                best[key] = c

        companions_sorted = sorted(
            best.values(), key=lambda x: x["score"], reverse=True
        )

        primary = [
            c["category_l2"]
            for c in companions_sorted
            if c["score"] >= primary_threshold
        ]
        allowed = [
            c["category_l2"]
            for c in companions_sorted
            if c["score"] >= allowed_threshold
        ]
        forbidden = [
            c["category_l2"]
            for c in companions_sorted
            if c["score"] < allowed_threshold
        ]

        # 找 anchor 属于哪个 role
        anchor_role = "unknown"
        for role, cats in cats_by_role.items():
            if anchor in cats:
                anchor_role = role
                break

        pairing_rules[anchor] = {
            "role": ROLE_CN.get(anchor_role, anchor_role),
            "primary": primary,
            "allowed": allowed,
        }

    return {
        "pairing_rules": pairing_rules,
    }


# ── CLI ──────────────────────────────────────────────────────────────

def parse_role_pairs(raw: str) -> list[tuple[str, str]]:
    """解析 --role-pairs 参数，如 'top:bottoms,top:shoes'。"""
    pairs = []
    for part in raw.split(","):
        part = part.strip()
        if ":" not in part:
            continue
        r1, r2 = part.split(":", 1)
        r1, r2 = r1.strip(), r2.strip()
        if r1 and r2:
            pairs.append((min(r1, r2), max(r1, r2)))
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate category_l2 Cartesian product pairing rules via LLM scoring",
    )
    parser.add_argument(
        "--input", type=Path, default=SKUS_JSONL,
        help="Path to skus.jsonl",
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help="Output YAML path",
    )
    parser.add_argument(
        "--role-pairs", type=str, default="",
        help="Comma-separated role pairs, e.g. 'top:bottoms,top:shoes'. Default: all pairs.",
    )
    parser.add_argument(
        "--min-sku-count", type=int, default=3,
        help="Minimum SKU count to include a category_l2 (default: 3)",
    )
    parser.add_argument(
        "--batch-size", type=int, default=50,
        help="Number of combinations per LLM call (default: 50)",
    )
    parser.add_argument(
        "--max-workers", type=int, default=3,
        help="Max parallel LLM calls (default: 3)",
    )
    parser.add_argument(
        "--primary-threshold", type=float, default=0.6,
        help="Score threshold for primary_companions (default: 0.6)",
    )
    parser.add_argument(
        "--allowed-threshold", type=float, default=0.4,
        help="Score threshold for allowed_companions (default: 0.4)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Only show combination counts, do not call LLM",
    )
    parser.add_argument(
        "--model", type=str, default=None,
        help="Override LLM model name (default: glm-5.2)",
    )
    args = parser.parse_args()

    # 1. 加载数据
    log.info("从 %s 加载 SKU 数据...", args.input)
    cats_by_role = load_categories_by_role(args.input, min_count=args.min_sku_count)
    for role in sorted(cats_by_role):
        log.info(
            "  %s (%s): %d 个中类",
            role, ROLE_CN.get(role, role), len(cats_by_role[role]),
        )

    # 2. 生成笛卡尔积组合
    role_pairs = parse_role_pairs(args.role_pairs) if args.role_pairs else None
    combos = generate_pairwise_combos(cats_by_role, role_pairs=role_pairs)
    log.info("笛卡尔积组合总数: %d", len(combos))

    # 按 role pair 统计
    pair_counts: Counter[str] = Counter()
    for c in combos:
        pair_counts[f"{c['role_1']} x {c['role_2']}"] += 1
    for pair, cnt in pair_counts.most_common():
        log.info("  %s: %d 组", pair, cnt)

    if args.dry_run:
        log.info("--dry-run 模式，不调用 LLM，退出")
        print(f"\n总计 {len(combos)} 个组合待打分")
        print(f"预计 LLM 调用次数: {(len(combos) + args.batch_size - 1) // args.batch_size}")
        return

    # 3. 调用 LLM 打分
    scores_map = score_all_combos(
        combos,
        batch_size=args.batch_size,
        max_workers=args.max_workers,
        model_override=args.model,
    )
    log.info("成功获取 %d / %d 个组合的得分", len(scores_map), len(combos))

    # 4. 组装并输出 YAML
    output = build_output(
        cats_by_role, combos, scores_map,
        primary_threshold=args.primary_threshold,
        allowed_threshold=args.allowed_threshold,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        f.write("# 中类(category_l2)搭配规则（LLM 笛卡尔积打分）\n")
        f.write(f"# 共 {len(output['pairing_rules'])} 个中类\n")
        f.write("# 字段: role(角色) / primary(首选搭配) / allowed(允许搭配)\n")
        f.write("# 生成脚本: scripts/gen_category_l2_cartesian_pairing.py\n\n")
        yaml.dump(
            output, f,
            allow_unicode=True,
            sort_keys=True,
            default_flow_style=False,
            width=120,
        )

    rules = output["pairing_rules"]
    total_primary = sum(len(r.get("primary", [])) for r in rules.values())
    total_allowed = sum(len(r.get("allowed", [])) for r in rules.values())
    log.info(
        "输出已写入 %s（%d 个中类, primary=%d, allowed=%d）",
        args.output,
        len(rules),
        total_primary,
        total_allowed,
    )


if __name__ == "__main__":
    main()
