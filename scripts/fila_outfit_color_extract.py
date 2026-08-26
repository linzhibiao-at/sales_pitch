#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FILA：为每个 SKU 找出**所有**「真人模特正面全身展示图」（含上身、下身、脚部），
并分别提取每张图中上装、下装、鞋的色系，输出 CSV（sku, image_url, json, reason, error）。

每个 SKU 可输出**多条**颜色搭配行：同一 SKU 的多张模特图可能展示不同的上下装/鞋
颜色搭配，逐张判断、各输出一条；同一颜色搭配（上装+下装+鞋色系完全一致）仅保留
一张代表图，避免重复。

复用 ``scripts/fila_images_preprocess.py`` 的全部基础设施（SKU/图片加载、图片下载
与编码、VLM 调用、并发与重试），仅新增一个 VLM 任务：单次调用同时「找全图 + 提色」。

数据来源、模型连接参数、CLI 与 ``fila_images_preprocess.py`` 对齐，详见该脚本。

色系词表对齐 ``backend/intent/dictionaries/color_series.yaml`` 的单品色系集合：
黑色系 / 白色系 / 灰色系 / 红色系 / 粉色系 / 橙色系 / 黄色系 / 绿色系 /
蓝色系 / 紫色系 / 棕色系 / 米色系 / 多色系。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tqdm import tqdm

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import fila_images_preprocess as ref  # noqa: E402

DEFAULT_CONFIG_PATH = ref.DEFAULT_CONFIG_PATH
DEFAULT_PRODUCT_DIR = ref.DEFAULT_PRODUCT_DIR
DEFAULT_SKUS_JSONL = ref.DEFAULT_SKUS_JSONL

MAX_RETRIES = ref.MAX_RETRIES
RETRY_DELAY_SEC = ref.RETRY_DELAY_SEC

# 色系词表（对齐 backend/intent/dictionaries/color_series.yaml 的单品色系）
COLOR_SERIES: List[str] = [
    "黑色系", "白色系", "灰色系", "红色系", "粉色系", "橙色系",
    "黄色系", "绿色系", "蓝色系", "紫色系", "棕色系", "米色系", "多色系",
]
_COLOR_SERIES_SET = set(COLOR_SERIES)

SYSTEM_PROMPT_OUTFIT = (
    "你是电商商品图识别助手，只根据用户给出的多张商品图做判断。"
    "你需要找出所有真人模特正面全身展示图（含上身、下身、脚部），"
    "并分别提取每张图中模特所穿上装、下装、鞋的色系。"
    "并严格按要求的 JSON 格式输出。"
)

USER_PROMPT_OUTFIT_TEMPLATE = """下面按顺序给出 {n} 张图片，编号从 1 到 {n}（第 1 张、第 2 张……）。

业务目标：为「货号 {attr_alias}」「款号 {style_no}」「颜色属性 id_pa={id_pa}」找出**所有**真人模特正面全身展示图（含上身、下身、脚部），并分别提取每张图中模特所穿上装、下装、鞋的色系。同一 SKU 的多张模特图可能展示不同的上下装/鞋颜色搭配，需逐张判断、各输出一条；不要只选一张。

**入选标准**（必须同时满足）：
1. 真人模特作为主体出镜的穿搭/展示图（不是平铺图、静物图、白底单品图、人台/假模图）。
2. 模特**正面**朝向镜头为主（微侧正面可接受；纯侧面、背面不算）。
3. 画面为**全身**：同时包含上身、下身、脚部（鞋子可见）；只拍到半身缺脚部、或脚部被裁掉的不算。
4. 商品展示完整清晰。

**排除**（满足任一条则不选）：
1. 非模特图：平铺/静物/白底单品图、人台/假模图、吊牌、水洗标、尺码表、成分说明、包装盒、赠品、配件细节特写。
2. 局部细节特写（领口、袖口、鞋底等），缺少商品整体轮廓。
3. 背面图、纯侧面图。
4. 拼图宫格主视觉、纯文字图。
5. 半身图、缺脚部的图。

**色系提取**（针对每张入选的图分别给出）：
- 分别给出模特所穿「上装」「下装」「鞋」的色系。
- **判定规则：以该部位面积最大的颜色（主色）为准**。若一件衣服有多种颜色，取占据视觉面积最大的那一种颜色作为该部位的色系，忽略小面积拼接、Logo、线条、装饰等次要颜色；不要取平均色或主观调和色。
- 面积相近时，取颜色更连续、块面更完整的那一种。
- 多色拼接若**没有明显主色**（各色面积相当，如大面积拼色块、撞色、扎染、满印、条纹/格纹铺满），才归为「多色系」；只要有明显主色，就归为主色对应的色系，不归多色系。
- 色系**只能**从以下词表中各选一个：黑色系、白色系、灰色系、红色系、粉色系、橙色系、黄色系、绿色系、蓝色系、紫色系、棕色系、米色系、多色系。
- 米色、奶色、燕麦色、杏色、卡其色、驼色等浅棕偏黄的中性色归为「米色系」；注意不要误归到黄色系或棕色系（米色系比黄色系更浅更灰、饱和度低，比棕色系更浅更不偏红）。
- 若某部位不可见、或模特未穿该部位（如未穿鞋、下装不可见），对应项填空字符串 ""。

请只做判断，不要编造不存在的编号。

严格只输出一个 JSON 对象（不要用 markdown 代码围栏），格式如下：
{{"outfits": [{{"chosen_index": <整数 1~{n}，表示该条对应第几张图>, "color": {{"上装": "<色系或空>", "下装": "<色系或空>", "鞋": "<色系或空>"}}, "reason": "<简短中文理由，说明该图为何入选及色系判定>"}}, ...]}}

若没有任何一张符合，outfits 为空数组 []。
"""


def _sanitize_color(raw: Any) -> str:
    """只允许词表内的色系，其余归一为空串。"""
    if not raw:
        return ""
    s = str(raw).strip()
    return s if s in _COLOR_SERIES_SET else ""


def _build_color_dict(raw: Any) -> Dict[str, str]:
    """从 VLM 输出的 color 字段构建 {上装,下装,鞋} 色系字典。"""
    out = {"上装": "", "下装": "", "鞋": ""}
    if isinstance(raw, dict):
        for k in out:
            out[k] = _sanitize_color(raw.get(k))
    return out


def call_vlm_extract_outfits(
    *,
    image_urls: List[str],
    attr_alias: str,
    style_no: str,
    id_pa: int,
    model: str,
    api_base: str,
    api_key: str,
    max_tokens: int,
    timeout_sec: float,
    enable_thinking: Optional[bool],
    max_pixels: int,
    min_pixels: int,
    image_quality: int,
) -> Tuple[List[Dict[str, Any]], str]:
    """单次调用：找出所有全身正面图 + 逐张提色。

    返回 ``(outfits, raw_text)``。``outfits`` 每个元素形如
    ``{"chosen_index": int(1..n), "color": {...}, "reason": str}``，按
    ``chosen_index`` 升序、且已对图片编号去重。无候选图时返回 ``([], "")``。
    """
    n = len(image_urls)
    if n == 0:
        return [], ""

    client = ref._create_chat_client(api_base, api_key, timeout_sec)
    image_content = ref._build_image_content(
        image_urls, max_pixels, min_pixels, image_quality
    )

    user_text = USER_PROMPT_OUTFIT_TEMPLATE.format(
        n=n,
        attr_alias=attr_alias,
        style_no=style_no,
        id_pa=id_pa,
    )

    raw_text = ref._call_vlm(
        client=client,
        model=model,
        system_prompt=SYSTEM_PROMPT_OUTFIT,
        user_text=user_text,
        image_content=image_content,
        max_tokens=max_tokens,
        enable_thinking=enable_thinking,
    )

    parsed = ref.parse_json_object(raw_text)
    if not parsed:
        return [], raw_text

    outfits_raw = parsed.get("outfits")
    if not isinstance(outfits_raw, list):
        return [], raw_text

    outfits: List[Dict[str, Any]] = []
    seen_idx: set[int] = set()
    for item in outfits_raw:
        if not isinstance(item, dict):
            continue
        idx = item.get("chosen_index")
        try:
            idx_int = int(idx)
        except (TypeError, ValueError):
            continue
        if idx_int < 1 or idx_int > n:
            continue
        if idx_int in seen_idx:
            continue
        seen_idx.add(idx_int)
        color = _build_color_dict(item.get("color"))
        reason = str(item.get("reason", "") or "").strip()
        outfits.append({"chosen_index": idx_int, "color": color, "reason": reason})

    outfits.sort(key=lambda o: o["chosen_index"])
    return outfits, raw_text


def process_one_sku(
    sku: Dict[str, Any],
    masters: Dict[int, str],
    images_by_goods: Dict[int, List[Dict[str, Any]]],
    args: argparse.Namespace,
) -> List[Dict[str, Any]]:
    """处理一个 SKU，返回**多条**颜色搭配行（每个不同颜色搭配一行）。

    无候选图或无符合的全身正面图时，返回单条仅含 error 的行。
    """
    gid = sku["id_goods"]
    id_pa = sku["id_pa"]
    attr_alias = sku["attr_alias"]
    style = masters.get(gid, "")

    cand = ref.collect_candidates(images_by_goods, gid, id_pa, args.max_candidates)
    cand_urls = [r["path"] for r in cand]

    def _err_row(error: str, reason: str = "") -> Dict[str, Any]:
        return {
            "sku": attr_alias,
            "image_url": "",
            "json": "",
            "reason": reason,
            "error": error,
        }

    if not cand:
        return [_err_row("无候选图（该 id_goods+id_pa 在 product_image 中无记录或全部为 empty 占位图）")]

    outfits: List[Dict[str, Any]] = []
    raw = ""
    err = ""
    for _ in range(MAX_RETRIES):
        try:
            outfits, raw = call_vlm_extract_outfits(
                image_urls=cand_urls,
                attr_alias=attr_alias,
                style_no=style,
                id_pa=id_pa,
                model=args.model,
                api_base=args.local_api_base,
                api_key=args.local_api_key,
                max_tokens=args.max_tokens,
                timeout_sec=args.timeout_sec,
                enable_thinking=args.enable_thinking,
                max_pixels=args.max_pixels,
                min_pixels=args.min_pixels,
                image_quality=args.image_quality,
            )
            break
        except Exception as exc:
            err = str(exc)
            time.sleep(RETRY_DELAY_SEC + random.random())

    if err and not outfits and not raw:
        return [_err_row(err)]

    # 同一颜色搭配（上装+下装+鞋色系完全一致）仅保留一张代表图（取编号最小的一张）。
    rows: List[Dict[str, Any]] = []
    seen_colors: set[Tuple[str, str, str]] = set()
    for o in outfits:
        color = o["color"]
        key = (color["上装"], color["下装"], color["鞋"])
        if key in seen_colors:
            continue
        seen_colors.add(key)
        rows.append({
            "sku": attr_alias,
            "image_url": cand[o["chosen_index"] - 1]["path"],
            "json": json.dumps(color, ensure_ascii=False),
            "reason": o["reason"],
            "error": "",
        })

    if not rows:
        return [_err_row("无真人模特正面全身图（outfits 为空）", reason="无真人模特正面全身图")]
    return rows


FIELDNAMES = ["sku", "image_url", "json", "reason", "error"]


def _write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDNAMES})


def _retry_mode(
    args: argparse.Namespace,
    masters: Dict[int, str],
    images_by_goods: Dict[int, List[Dict[str, Any]]],
    skus: List[Dict[str, Any]],
) -> int:
    import csv

    original = ref.read_csv_dicts(args.output)
    err_aliases: set[str] = set()
    for row in original:
        alias = (row.get("sku") or "").strip()
        if not alias:
            continue
        if (row.get("error") or "").strip() and not (row.get("image_url") or "").strip():
            err_aliases.add(alias)
    print(f"已有 CSV 总行数: {len(original)}, 待重试货号数: {len(err_aliases)}")
    skus = [s for s in skus if s["attr_alias"] in err_aliases]
    missing = err_aliases - {s["attr_alias"] for s in skus}
    if missing:
        print(f"警告：以下出错货号在当前 SKU 列表中未找到，将跳过: {sorted(missing)}", file=sys.stderr)
    print(f"待重试 SKU 条数: {len(skus)}")

    results: Dict[str, List[Dict[str, Any]]] = {}
    lock = threading.Lock()

    def worker(sku: Dict[str, Any]) -> None:
        rs = process_one_sku(sku, masters, images_by_goods, args)
        alias = str(rs[0].get("sku") or "") if rs else ""
        with lock:
            results[alias] = rs

    with ThreadPoolExecutor(max_workers=max(1, args.threads)) as ex:
        futs = {ex.submit(worker, s): s for s in skus}
        with tqdm(total=len(skus), unit="sku") as bar:
            for fut in as_completed(futs):
                fut.result()
                bar.update(1)

    merged: List[Dict[str, Any]] = []
    fixed = 0
    replaced: set[str] = set()
    for row in original:
        alias = (row.get("sku") or "").strip()
        if alias not in results:
            merged.append(row)
            continue
        # 该货号本次已重试：在首次出现处插入新行，后续旧行全部丢弃
        if alias in replaced:
            continue
        replaced.add(alias)
        new_rows = results[alias]
        if any(str(r.get("image_url", "")).strip() for r in new_rows):
            for r in new_rows:
                merged.append({k: r.get(k, "") for k in FIELDNAMES})
            fixed += 1
        else:
            # 仍失败：保留原行，仅更新 error/reason
            row["error"] = str(new_rows[0].get("error", "")) if new_rows else ""
            row["reason"] = str(new_rows[0].get("reason", "")) if new_rows else ""
            merged.append(row)

    _write_csv(args.output, merged)
    ok = sum(1 for r in merged if str(r.get("image_url", "")).strip())
    ok_skus = len({r.get("sku") for r in merged if str(r.get("image_url", "")).strip()})
    print(
        f"重试完成。输出: {args.output}  本次修复货号: {fixed}  "
        f"最终颜色搭配行: {ok}/{len(merged)}（覆盖 {ok_skus} 个货号）"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "FILA：为每个 SKU 找出所有真人模特正面全身图（含上身/下身/脚部），"
            "并逐张提取上装/下装/鞋色系，输出 sku,image_url,json,reason,error CSV"
            "（每个 SKU 可输出多条颜色搭配行）"
        ),
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--product-dir", type=Path, default=DEFAULT_PRODUCT_DIR)
    parser.add_argument("--source", choices=["catalog", "onsale"], default="catalog")
    parser.add_argument("--skus-jsonl", type=Path, default=DEFAULT_SKUS_JSONL)
    parser.add_argument("--local-api-base", type=str, default=None)
    parser.add_argument("--local-api-key", type=str, default=None)
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument("--timeout-sec", type=float, default=None)
    parser.add_argument("--enable-thinking", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_PRODUCT_DIR / "fila_sku_outfit_colors.csv",
        help="输出 CSV 路径",
    )
    parser.add_argument("--sku-id", type=str, default=None, help="仅处理指定货号，逗号分隔")
    parser.add_argument("--articles", type=Path, default=None, help="每行一个货号的文件")
    parser.add_argument("--limit", type=int, default=0, help="仅处理前 N 条（0=全部）")
    parser.add_argument("--max-candidates", type=int, default=48)
    parser.add_argument("--threads", type=int, default=40)
    parser.add_argument("--max-pixels", type=int, default=ref.DEFAULT_MAX_PIXELS)
    parser.add_argument("--min-pixels", type=int, default=ref.DEFAULT_MIN_PIXELS)
    parser.add_argument("--image-quality", type=int, default=ref.DEFAULT_IMAGE_QUALITY)
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        default=False,
        help="从已有输出 CSV 读取出错行（image_url 空 且 error 非空），仅重试这些 SKU 并就地更新",
    )

    args = parser.parse_args()
    vision = ref.resolve_vision_llm_settings(args.config)
    args.local_api_base = args.local_api_base or vision["api_base"]
    args.local_api_key = args.local_api_key or vision["api_key"]
    args.model = args.model or vision["model"]
    args.max_tokens = args.max_tokens if args.max_tokens is not None else vision["max_tokens"]
    args.timeout_sec = args.timeout_sec if args.timeout_sec is not None else vision["timeout_sec"]
    if args.enable_thinking is None:
        args.enable_thinking = vision["enable_thinking"]

    product_dir = args.product_dir
    if not product_dir.is_dir():
        print(f"错误：目录不存在 {product_dir}", file=sys.stderr)
        return 1
    master_path = product_dir / "product_master.csv"
    if not master_path.is_file():
        print(f"错误：缺少 {master_path}", file=sys.stderr)
        return 1

    masters = ref.load_masters(master_path)
    images_by_goods = ref.load_images_by_goods(product_dir)

    if args.source == "onsale":
        skus = ref.load_onsale_skus(product_dir)
        print(f"全量在售 SKU: {len(skus)}")
    else:
        if not args.skus_jsonl.is_file():
            print(f"错误：缺少 {args.skus_jsonl}，请先运行 build_catalog.py", file=sys.stderr)
            return 1
        skus = ref.load_catalog_skus(args.skus_jsonl)
        print(f"catalog SKU: {len(skus)}  来源: {args.skus_jsonl}")

    if not args.local_api_base:
        print("错误：models.vision_llm.base_url 未配置", file=sys.stderr)
        return 1
    if not args.local_api_key:
        print(f"错误：未设置 API Key，请 export {vision['api_key_env']}", file=sys.stderr)
        return 1

    venv_hint = (
        f"已激活虚拟环境: {os.environ.get('VIRTUAL_ENV')}"
        if os.environ.get("VIRTUAL_ENV")
        else "未检测到 VIRTUAL_ENV"
    )
    print(f"vision_llm 配置来源: {args.config}  ({venv_hint})")
    print(
        f"模型: {args.model}  API: {args.local_api_base}  "
        f"max_tokens: {args.max_tokens}  timeout: {args.timeout_sec}s  "
        f"enable_thinking: {args.enable_thinking}"
    )

    if args.sku_id:
        wanted = ref.parse_sku_id_arg(args.sku_id)
        all_aliases = {s["attr_alias"] for s in skus}
        missing = wanted - all_aliases
        if missing:
            print(f"警告：以下 sku_id 未找到: {sorted(missing)}", file=sys.stderr)
        skus = [s for s in skus if s["attr_alias"] in wanted]
        print(f"按 --sku-id 过滤: {len(skus)} 条")

    if args.articles and args.articles.is_file():
        wanted = {x.strip() for x in args.articles.read_text(encoding="utf-8").splitlines() if x.strip()}
        skus = [s for s in skus if s["attr_alias"] in wanted]

    if args.limit and args.limit > 0:
        skus = skus[: args.limit]

    print(f"待处理 SKU 条数: {len(skus)}")

    if args.retry_errors:
        if not args.output.is_file():
            print(f"错误：--retry-errors 需要已有输出文件 {args.output}", file=sys.stderr)
            return 1
        return _retry_mode(args, masters, images_by_goods, skus)

    results: List[Optional[List[Dict[str, Any]]]] = [None] * len(skus)
    lock = threading.Lock()

    def worker(ii: int, sku: Dict[str, Any]) -> None:
        r = process_one_sku(sku, masters, images_by_goods, args)
        with lock:
            results[ii] = r

    with ThreadPoolExecutor(max_workers=max(1, args.threads)) as ex:
        futs = {ex.submit(worker, i, s): i for i, s in enumerate(skus)}
        with tqdm(total=len(skus), unit="sku") as bar:
            for fut in as_completed(futs):
                fut.result()
                bar.update(1)

    rows: List[Dict[str, Any]] = []
    for r in results:
        if r:
            rows.extend(r)
    _write_csv(args.output, rows)
    ok = sum(1 for r in rows if str(r.get("image_url", "")).strip())
    err_count = sum(1 for r in rows if str(r.get("error", "")).strip())
    ok_skus = len({r.get("sku") for r in rows if str(r.get("image_url", "")).strip()})
    print(
        f"完成。输出: {args.output}  颜色搭配行: {ok}/{len(rows)}  "
        f"（覆盖 {ok_skus} 个货号；错误/无图行: {err_count}）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
