#!/usr/bin/env python3
"""FILA：用 VLM 为 length_class=n/a 的上装/下装补长短款。

背景
----
``backend/intent/sku_attributes.py: extract_length_class`` 靠 category_l2 + title
关键词推导长短款（long/short/n/a）。约 1100 个 top/bottoms SKU 两条信号都没有，
落 n/a。源表里没有任何结构化袖长/裤长/裙长字段（见排查记录），唯一真实信号是
商品图本身。本脚本用已选好的 ``tryon_image`` 调 VLM 判定长短款，结果写入
``data/processed/sku_length_vlm.csv``，后续 ETL 读这个 CSV 把 n/a 补上（不覆盖
已有 short/long，集成步骤见 plan）。

只处理 role ∈ {top, bottoms} 且 length_class ∈ {n/a, 空, 缺失} 且 tryon_image
非空的 SKU。复用 ``scripts/fila_images_preprocess.py`` 的 VLM 工具链与 config
``models.vision_llm``，不重复造轮子。

用法
----
    cd fila_agent_html && export PYTHONPATH="$(pwd)"
    # 单条冒烟
    python scripts/extract_length_class_vlm.py --sku-id T11W615501FWI --threads 1
    # 全量（约 1100 条），默认续跑：已成功的不重跑，失败的重试
    python scripts/extract_length_class_vlm.py --threads 4
    # 强制全重跑
    python scripts/extract_length_class_vlm.py --force
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

from tqdm import tqdm

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 复用 fila_images_preprocess 的 VLM 工具链（不重造轮子）
from scripts.fila_images_preprocess import (  # noqa: E402
    DEFAULT_IMAGE_QUALITY,
    DEFAULT_MAX_PIXELS,
    DEFAULT_MIN_PIXELS,
    _build_image_content,
    _call_vlm,
    _create_chat_client,
    parse_json_object,
    resolve_vision_llm_settings,
)

from backend.intent.sku_attributes import is_swimwear  # noqa: E402

logger = logging.getLogger("extract_length_class_vlm")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

DEFAULT_SKUS_JSONL = ROOT / "data" / "processed" / "skus.jsonl"
DEFAULT_OUTPUT = ROOT / "data" / "processed" / "sku_length_vlm.csv"

# 仅这些 role 的 length_class 有意义；其余（鞋/配饰/连衣裙）本就 n/a，不跑 VLM
TARGET_ROLES = {"top", "bottoms"}
VALID_LENGTH_CLASS = {"long", "short", "n/a"}

ROLE_CN = {"top": "上装", "bottoms": "下装"}

FIELDNAMES = [
    "sku_id",
    "spu_id",
    "role",
    "category_l1",
    "category_l2",
    "category_l3",
    "title",
    "tryon_image",
    "length_class",
    "sleeve_length",
    "garment_length",
    "confidence",
    "reason",
    "error",
    "raw_response_tail",
    "ts",
]

SYSTEM_PROMPT = (
    "你是电商商品图属性识别助手。只根据给定的一张商品图判断目标商品的"
    "袖长/裤长/裙长，严格按要求的 JSON 格式输出，不要输出任何额外文字。"
)

USER_PROMPT_TEMPLATE = """下面给出 1 张商品图（tryon 图，可能由真人模特穿着）。

目标商品：货号 {sku_id}，类别 {category_l1} / {category_l2} / {category_l3}，{role_cn}（商品名称：{title}）。
图中若有模特，请聚焦**该目标商品**本身判断，忽略模特本人及其他搭配单品。

判定规则：
- 上装：按袖长判定 length_class —— 长袖→"long"；短袖/无袖→"short"；难以判定→"n/a"
- 下装：按裤长/裙长判定 —— 长裤/长裙→"long"；短裤/五分/七分裤/短裙→"short"；难以判定→"n/a"

严格只输出一个 JSON 对象（不要用 markdown 代码围栏，不要解释），格式：
{{"length_class":"long|short|n/a","sleeve_length":"长袖|短袖|无袖|七分袖|n/a","garment_length":"长款|短款|中款|n/a","confidence":0到1之间的小数,"reason":"<简短中文理由>"}}
"""


def load_skus(path: Path) -> list[dict[str, Any]]:
    """读 skus.jsonl，返回全部 SKU dict。"""
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def filter_targets(
    skus: list[dict[str, Any]],
    sku_id_filter: Optional[set[str]],
    limit: int,
) -> list[dict[str, Any]]:
    """筛出需要跑 VLM 的 SKU：role∈{top,bottoms} 且 length_class 为 n/a/空/缺失 且有 tryon_image。"""
    out: list[dict[str, Any]] = []
    for s in skus:
        role = str(s.get("role") or "").strip().lower()
        if role not in TARGET_ROLES:
            continue
        # 泳装跳过：length 在沙滩域不作 season 代理，VLM 按视觉长短判定反而
        # 重新触发「长袖×短款下装季节冲突」（如 开衫泳衣×短裤泳装）。
        if is_swimwear(s.get("category_l2"), s.get("title")):
            continue
        lc = str(s.get("length_class") or "").strip()
        if lc and lc != "n/a":
            # 已有 short/long，不补（用户要求仅填 n/a）
            continue
        tryon = str(s.get("tryon_image") or "").strip()
        if not tryon:
            continue
        if sku_id_filter and str(s.get("sku_id") or "") not in sku_id_filter:
            continue
        out.append(s)
        if limit and len(out) >= limit:
            break
    return out


def load_done_sku_ids(output: Path) -> set[str]:
    """读已存在的输出 CSV，返回已**成功**的 sku_id 集合（error 空 且 length_class 非空），用于续跑跳过。"""
    if not output.is_file():
        return set()
    done: set[str] = set()
    try:
        with output.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if not row.get("error") and row.get("length_class"):
                    sid = (row.get("sku_id") or "").strip()
                    if sid:
                        done.add(sid)
    except Exception as exc:
        logger.warning("读取已存在 CSV 失败，忽略续跑: %s", exc)
        return set()
    return done


def _empty_row(sku: dict[str, Any]) -> dict[str, Any]:
    """构造一个待填充的输出行（含 SKU 上下文，result 字段留空）。"""
    return {
        "sku_id": str(sku.get("sku_id") or ""),
        "spu_id": str(sku.get("spu_id") or ""),
        "role": str(sku.get("role") or ""),
        "category_l1": str(sku.get("category_l1") or ""),
        "category_l2": str(sku.get("category_l2") or ""),
        "category_l3": str(sku.get("category_l3") or ""),
        "title": str(sku.get("title") or ""),
        "tryon_image": str(sku.get("tryon_image") or ""),
        "length_class": "",
        "sleeve_length": "",
        "garment_length": "",
        "confidence": "",
        "reason": "",
        "error": "",
        "raw_response_tail": "",
        "ts": "",
    }


def process_one(
    sku: dict[str, Any],
    vlm_settings: dict[str, Any],
    client,
    max_pixels: int,
    min_pixels: int,
    image_quality: int,
) -> dict[str, Any]:
    """处理单个 SKU：下载 tryon_image → 调 VLM → 解析 → 返回输出行。失败在 error 列记原因。"""
    row = _empty_row(sku)
    row["ts"] = str(int(time.time()))
    tryon = row["tryon_image"]

    # 1. 下载+编码图片（失败也照常发给 VLM，_build_image_content 会放占位文本）
    try:
        image_content = _build_image_content(
            [tryon], max_pixels, min_pixels, image_quality,
        )
    except Exception as exc:
        row["error"] = f"image_fetch_failed:{exc}"[:200]
        return row
    if not any(c.get("type") == "image_url" for c in image_content):
        row["error"] = "image_fetch_failed"
        return row

    # 2. 调 VLM
    user_text = USER_PROMPT_TEMPLATE.format(
        sku_id=row["sku_id"],
        category_l1=row["category_l1"],
        category_l2=row["category_l2"],
        category_l3=row["category_l3"],
        role_cn=ROLE_CN.get(row["role"], row["role"]),
        title=row["title"],
    )
    try:
        raw = _call_vlm(
            client=client,
            model=vlm_settings["model"],
            system_prompt=SYSTEM_PROMPT,
            user_text=user_text,
            image_content=image_content,
            max_tokens=vlm_settings["max_tokens"],
            enable_thinking=vlm_settings["enable_thinking"],
        )
    except Exception as exc:
        row["error"] = f"vlm_call_failed:{exc}"[:200]
        return row

    # 折叠换行避免 CSV 单条记录跨多行（便于人眼/Excel 查看），保留末尾 ~300 字符
    row["raw_response_tail"] = " ".join((raw or "").split())[-300:]

    # 3. 解析 JSON
    parsed = parse_json_object(raw or "")
    if not parsed:
        row["error"] = "json_parse_failed"
        return row

    lc = str(parsed.get("length_class") or "").strip().lower()
    if lc not in VALID_LENGTH_CLASS:
        row["error"] = f"invalid_length_class:{lc}"[:200]
        # 仍记录原始字段便于排查
        row["sleeve_length"] = str(parsed.get("sleeve_length") or "")[:32]
        row["garment_length"] = str(parsed.get("garment_length") or "")[:32]
        return row

    row["length_class"] = lc
    row["sleeve_length"] = str(parsed.get("sleeve_length") or "")[:32]
    row["garment_length"] = str(parsed.get("garment_length") or "")[:32]
    try:
        row["confidence"] = str(round(float(parsed.get("confidence") or 0), 3))
    except (TypeError, ValueError):
        row["confidence"] = ""
    row["reason"] = str(parsed.get("reason") or "")[:200]
    return row


class CsvAppender:
    """线程安全地 append 结果行到 CSV；首行写 header。每 N 行 flush 一次。"""

    def __init__(self, path: Path, fieldnames: list[str], flush_every: int = 50) -> None:
        self.path = path
        self.fieldnames = fieldnames
        self.flush_every = flush_every
        self.lock = threading.Lock()
        self._count = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists() or path.stat().st_size == 0
        self._fh = path.open("a", encoding="utf-8-sig", newline="")
        self._writer = csv.DictWriter(self._fh, fieldnames=fieldnames)
        if new_file:
            self._writer.writeheader()
            self._fh.flush()

    def append(self, row: dict[str, Any]) -> None:
        with self.lock:
            self._writer.writerow(row)
            self._count += 1
            if self._count % self.flush_every == 0:
                self._fh.flush()

    def close(self) -> None:
        with self.lock:
            try:
                self._fh.flush()
            finally:
                self._fh.close()


def build_vlm_settings(args: argparse.Namespace) -> dict[str, Any]:
    """读 config models.vision_llm，再用 CLI 覆盖参数替换。"""
    settings = resolve_vision_llm_settings()
    if args.local_api_base:
        settings["api_base"] = args.local_api_base.rstrip("/")
    if args.local_api_key:
        settings["api_key"] = args.local_api_key
    if args.model:
        settings["model"] = args.model
    if args.max_tokens is not None:
        settings["max_tokens"] = args.max_tokens
    if args.timeout_sec is not None:
        settings["timeout_sec"] = args.timeout_sec
    if args.enable_thinking is not None:
        settings["enable_thinking"] = args.enable_thinking
    return settings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="用 VLM 为 length_class=n/a 的上装/下装补长短款，输出 CSV 供后续 ETL 回填",
    )
    parser.add_argument("--skus-jsonl", type=Path, default=DEFAULT_SKUS_JSONL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0, help="仅处理前 N 条（0=全部）")
    parser.add_argument("--sku-id", type=str, default=None, help="仅处理指定货号，逗号分隔")
    parser.add_argument("--force", action="store_true", help="忽略续跑，全部重跑（追加写）")
    # 图片参数
    parser.add_argument("--max-pixels", type=int, default=DEFAULT_MAX_PIXELS)
    parser.add_argument("--min-pixels", type=int, default=DEFAULT_MIN_PIXELS)
    parser.add_argument("--image-quality", type=int, default=DEFAULT_IMAGE_QUALITY)
    # VLM 覆盖组（默认读 config models.vision_llm）
    parser.add_argument("--local-api-base", type=str, default=None)
    parser.add_argument("--local-api-key", type=str, default=None)
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument("--timeout-sec", type=float, default=None)
    parser.add_argument(
        "--enable-thinking",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    args = parser.parse_args()

    if not args.skus_jsonl.is_file():
        print(f"缺少 {args.skus_jsonl}", file=sys.stderr)
        return 1

    vlm = build_vlm_settings(args)
    if not vlm.get("api_base") or not vlm.get("api_key"):
        print(
            "VLM 未配置：缺少 models.vision_llm.base_url 或 api_key_env 对应环境变量",
            file=sys.stderr,
        )
        return 1
    logger.info(
        "VLM: model=%s base=%s max_tokens=%s timeout=%s thinking=%s",
        vlm["model"], vlm["api_base"], vlm["max_tokens"],
        vlm["timeout_sec"], vlm["enable_thinking"],
    )

    sku_id_filter: Optional[set[str]] = None
    if args.sku_id:
        sku_id_filter = {s.strip() for s in args.sku_id.split(",") if s.strip()}

    # 续跑：跳过已成功的
    done_ids: set[str] = set() if args.force else load_done_sku_ids(args.output)
    if done_ids:
        logger.info("续跑：跳过已成功的 %d 条", len(done_ids))

    skus = load_skus(args.skus_jsonl)
    targets = filter_targets(skus, sku_id_filter, args.limit)
    targets = [s for s in targets if str(s.get("sku_id") or "") not in done_ids]
    if not targets:
        logger.info("无可处理 SKU（全部已成功或无 n/a 上装/下装带图）")
        return 0
    logger.info("待处理 SKU: %d 条", len(targets))

    client = _create_chat_client(
        vlm["api_base"], vlm["api_key"], vlm["timeout_sec"],
    )
    appender = CsvAppender(args.output, FIELDNAMES)
    counter = {"ok": 0, "err": 0}
    counter_lock = threading.Lock()

    def worker(sku: dict[str, Any]) -> dict[str, Any]:
        row = process_one(
            sku, vlm, client,
            args.max_pixels, args.min_pixels, args.image_quality,
        )
        appender.append(row)
        with counter_lock:
            if row["error"]:
                counter["err"] += 1
            else:
                counter["ok"] += 1
        return row

    with ThreadPoolExecutor(max_workers=max(1, args.threads)) as ex:
        futs = {ex.submit(worker, s): s for s in targets}
        with tqdm(total=len(futs), unit="sku", desc="vlm length") as bar:
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.exception("worker 异常: %s", exc)
                bar.update(1)

    appender.close()
    logger.info(
        "完成：成功 %d，失败 %d，输出 %s",
        counter["ok"], counter["err"], args.output,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
