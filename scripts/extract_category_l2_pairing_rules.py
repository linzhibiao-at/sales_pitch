#!/usr/bin/env python3
"""从 FILA 固定搭配数据归纳中类(category_l2)搭配规则，输出 YAML。"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

import yaml

_SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = _SCRIPT_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._project_paths import load_paths
from scripts.etl_common import normalize_category_l2

_PATHS = load_paths()
OUTFITS_PATH = _PATHS["outfits_json"]
DEFAULT_OUTPUT = (
    ROOT / "backend" / "intent" / "dictionaries" / "category_l2_pairing.yaml"
)
_EXCL_PATH = ROOT / "backend" / "intent" / "dictionaries" / "non_clothing_exclusion.yaml"

# 中类 -> 搭配角色（使用归一化后的标准名）
CATEGORY_ROLE = {
    # 上装
    "短袖T恤": "上装",
    "短袖POLO": "上装",
    "长袖T恤": "上装",
    "长袖POLO": "上装",
    "短袖衬衫": "上装",
    "长袖衬衫": "上装",
    "套头卫衣": "上装",
    "连帽卫衣": "上装",
    "编织衫": "上装",
    "短袖编织衫": "上装",
    "短袖梭织上衣": "上装",
    "短袖针织上衣": "上装",
    "编织开衫": "上装",
    "针织上衣": "上装",
    "梭织上衣": "上装",
    "背心": "上装",
    # 外套
    "梭织外套": "外套",
    "棉服": "外套",
    "毛呢上衣": "外套",
    "羽绒服": "外套",
    "中长羽绒服": "外套",
    "长羽绒服": "外套",
    "羽绒马甲": "外套",
    "梭织马甲": "外套",
    "针织马甲": "外套",
    "单层冲锋衣": "外套",
    "防晒服": "外套",
    # 下装
    "梭织长裤": "下装",
    "梭织短裤": "下装",
    "梭织五分裤": "下装",
    "梭织七分裤": "下装",
    "针织长裤": "下装",
    "针织短裤": "下装",
    "针织七分裤": "下装",
    "针织打底裤": "下装",
    "半身裙": "下装",
    "裤裙": "下装",
    "背带裙": "下装",
    "背带裤": "下装",
    "连衣裙": "连衣裙",
    # 鞋
    "老爹鞋": "鞋",
    "户外鞋": "鞋",
    "跑鞋": "鞋",
    "休闲鞋": "鞋",
    "板鞋": "鞋",
    "帆布鞋": "鞋",
    "网球鞋": "鞋",
    "高尔夫鞋": "鞋",
    "篮球鞋": "鞋",
    "运动鞋": "鞋",
    "潮鞋": "鞋",
    "凉鞋": "鞋",
    "拖鞋": "鞋",
    "儿童鞋": "鞋",
    # 配饰
    "帽子": "配饰",
    "手套": "配饰",
    "腰带": "配饰",
    "袜子": "配饰",
    "渔夫帽": "配饰",
    "时装帽": "配饰",
    # 包
    "包": "包",
}

PRIMARY_RATE = 0.08
ALLOWED_RATE = 0.02

def _load_excluded_categories() -> set[str]:
    """从 non_clothing_exclusion.yaml 加载不参与搭配的中类。"""
    if not _EXCL_PATH.is_file():
        return set()
    with _EXCL_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return set(data.get("non_clothing", []) + data.get("intimate_swimwear", []))


# 全局排除：非服饰 / 内衣泳衣 / 赠品配件
EXCLUDED_CATEGORIES: set[str] = _load_excluded_categories()

# 连衣裙类品类：一体式服装，不应 required 上装
DRESS_CATEGORIES: set[str] = {"连衣裙"}


def load_outfits(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def outfit_categories(outfit: dict) -> list[str]:
    cats: list[str] = []
    for item in outfit.get("items") or []:
        cat = normalize_category_l2(str(item.get("category_l2") or "").strip())
        if cat:
            cats.append(cat)
    return cats


def master_categories(outfit: dict) -> set[str]:
    masters: set[str] = set()
    for item in outfit.get("items") or []:
        cat = normalize_category_l2(str(item.get("category_l2") or "").strip())
        if cat and item.get("isMaster"):
            masters.add(cat)
    if not masters:
        cats = outfit_categories(outfit)
        if cats:
            masters.add(cats[0])
    return masters


def confidence(rate: float) -> str:
    if rate >= 0.20:
        return "high"
    if rate >= PRIMARY_RATE:
        return "medium"
    if rate >= ALLOWED_RATE:
        return "low"
    return "rare"


def build_rules(outfits: list[dict]) -> dict:
    all_cats: Counter[str] = Counter()
    cat_updown: dict[str, Counter[str]] = defaultdict(Counter)
    cat_cattype: dict[str, Counter[str]] = defaultdict(Counter)

    co_occurrence: dict[str, Counter[str]] = defaultdict(Counter)
    master_companion: dict[str, Counter[str]] = defaultdict(Counter)
    master_occurrences: Counter[str] = Counter()
    role_patterns: Counter[str] = Counter()

    analyzed_outfits = 0
    for outfit in outfits:
        items = outfit.get("items") or []
        cats_in_outfit: list[str] = []
        for item in items:
            cat = normalize_category_l2(str(item.get("category_l2") or "").strip())
            if not cat:
                continue
            all_cats[cat] += 1
            cats_in_outfit.append(cat)
            attrs = item.get("attributes") or {}
            ud = str(attrs.get("upDown") or "").strip()
            ct = str(attrs.get("catType") or "").strip()
            if ud:
                cat_updown[cat][ud] += 1
            if ct:
                cat_cattype[cat][ct] += 1

        unique_cats = sorted(set(cats_in_outfit))
        if len(unique_cats) < 2:
            continue
        analyzed_outfits += 1

        for a, b in combinations(unique_cats, 2):
            co_occurrence[a][b] += 1
            co_occurrence[b][a] += 1

        roles = sorted(
            {CATEGORY_ROLE[c] for c in unique_cats if c in CATEGORY_ROLE}
        )
        if len(roles) >= 2:
            role_patterns["+".join(roles)] += 1

        masters = master_categories(outfit)
        for master in masters:
            master_occurrences[master] += 1
            for cat in unique_cats:
                if cat != master:
                    master_companion[master][cat] += 1

    all_category_names = sorted(
        c for c in all_cats.keys() if c not in EXCLUDED_CATEGORIES
    )

    category_meta = {}
    for cat in all_category_names:
        up_down = ""
        if cat_updown[cat]:
            up_down = cat_updown[cat].most_common(1)[0][0]
        cat_type = ""
        if cat_cattype[cat]:
            cat_type = cat_cattype[cat].most_common(1)[0][0]
        category_meta[cat] = {
            "role": CATEGORY_ROLE.get(cat, "unknown"),
            "up_down": up_down or None,
            "cat_type": cat_type or None,
            "item_count": int(all_cats[cat]),
        }

    pairing_rules = {}
    for anchor in all_category_names:
        anchor_total = int(master_occurrences.get(anchor, 0))
        comp_counter = master_companion.get(anchor, Counter())
        allowed: list[str] = []
        primary: list[str] = []

        anchor_role = CATEGORY_ROLE.get(anchor, "unknown")
        for comp in all_category_names:
            if comp == anchor:
                continue
            # 同角色品类不应搭配（如下装+下装）
            comp_role = CATEGORY_ROLE.get(comp, "unknown")
            if (
                anchor_role != "unknown"
                and comp_role != "unknown"
                and anchor_role == comp_role
            ):
                continue
            count = int(comp_counter.get(comp, 0))
            rate = (count / anchor_total) if anchor_total else 0.0
            co_count = int(co_occurrence.get(anchor, Counter()).get(comp, 0))
            co_rate = (
                co_count / analyzed_outfits if analyzed_outfits else 0.0
            )
            if count == 0 and co_count == 0:
                continue
            if rate >= ALLOWED_RATE or co_rate >= ALLOWED_RATE:
                allowed.append(comp)
            if rate >= PRIMARY_RATE or co_rate >= PRIMARY_RATE:
                primary.append(comp)

        slim: dict[str, object] = {"role": anchor_role}
        if anchor_total:
            slim["anchor_count"] = anchor_total
        if primary:
            slim["primary"] = primary
        if allowed:
            slim["allowed"] = allowed
        pairing_rules[anchor] = slim

    return {
        "pairing_rules": pairing_rules,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract category_l2 pairing rules from FILA outfits",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=OUTFITS_PATH,
        help="Path to fila_outfits.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Output YAML path",
    )
    args = parser.parse_args()

    outfits = load_outfits(args.input)
    rules = build_rules(outfits)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        f.write("# 中类(category_l2)搭配规则（共现统计）\n")
        f.write(f"# 共 {len(rules['pairing_rules'])} 个中类\n")
        f.write("# 字段: role(角色) / anchor_count(锚点统计数) / primary(首选搭配) / allowed(允许搭配)\n")
        f.write("# 生成脚本: scripts/extract_category_l2_pairing_rules.py\n\n")
        yaml.dump(
            rules,
            f,
            allow_unicode=True,
            sort_keys=True,
            default_flow_style=False,
            width=100,
        )

    print(
        f"Wrote pairing rules to {args.output} "
        f"({len(rules['pairing_rules'])} categories)"
    )


if __name__ == "__main__":
    main()
