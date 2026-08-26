#!/usr/bin/env python3
"""就地回补 skus.jsonl 里 length_class=n/a 的上装/下装，用 VLM CSV 的判定值。

背景
----
``extract_length_class`` 靠 category_l2 + title 推导，约 1100 个 top/bottoms 落 n/a。
``scripts/extract_length_class_vlm.py`` 已用 VLM 对这些 n/a 的 tryon_image 判出长短款，
写入 ``data/processed/sku_length_vlm.csv``。本脚本读该 CSV，把 skus.jsonl 里仍是 n/a
的 length_class 回填为 VLM 值（仅填 n/a，不覆盖已有 short/long）。

ETL 构建侧（``scripts/etl_common.py: resolve_length_class``）已同样接入 VLM CSV，
所以未来重跑 ETL 重建 skus.jsonl 时回补会自动保留；本脚本只是一次性桥接，让当前
skus.jsonl 不必重跑全量 ETL 即刻生效。

用法
----
    cd fila_agent_html && export PYTHONPATH="$(pwd)"
    python scripts/backfill_length_class_vlm.py            # 回补并覆盖写回
    python scripts/backfill_length_class_vlm.py --dry-run  # 只统计不改文件
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.etl_common import load_length_class_vlm_index  # noqa: E402
from backend.intent.sku_attributes import is_swimwear  # noqa: E402

DEFAULT_SKUS_JSONL = ROOT / "data" / "processed" / "skus.jsonl"


def backfill(path: Path, dry_run: bool) -> int:
    vlm = load_length_class_vlm_index()
    if not vlm:
        print(f"VLM CSV 为空或不存在（{path.parent / 'sku_length_vlm.csv'}），无可回补值。")
        return 1

    rows: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    before = Counter(str(r.get("length_class") or "") or "<missing>" for r in rows)
    filled = 0
    skipped_has_value = 0
    skipped_no_vlm = 0
    for r in rows:
        role = str(r.get("role") or "").strip().lower()
        if role not in {"top", "bottoms"}:
            continue
        # 泳装跳过：length 不作 season 代理，保留 n/a（避免误杀沙滩搭配）
        if is_swimwear(r.get("category_l2"), r.get("title")):
            continue
        cur = str(r.get("length_class") or "").strip()
        if cur and cur != "n/a":
            # 已有 short/long，不覆盖
            skipped_has_value += 1
            continue
        sid = str(r.get("sku_id") or "")
        v = vlm.get(sid)
        if v in ("long", "short"):
            r["length_class"] = v
            filled += 1
        else:
            skipped_no_vlm += 1

    after = Counter(str(r.get("length_class") or "") or "<missing>" for r in rows)

    print(f"总 SKU: {len(rows)}")
    print(f"VLM CSV 可用回补值: {len(vlm)} 条")
    print(f"回补(n/a → long/short): {filled}")
    print(f"跳过(已有 short/long 不覆盖): {skipped_has_value}")
    print(f"跳过(VLM 也判 n/a 或未跑): {skipped_no_vlm}")
    print(f"length_class 变化: {dict(before)} → {dict(after)}")

    if dry_run:
        print("--dry-run：未写回文件。")
    else:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        tmp.replace(path)
        print(f"已写回 {path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="用 VLM CSV 回补 skus.jsonl 的 length_class=n/a")
    parser.add_argument("--skus-jsonl", type=Path, default=DEFAULT_SKUS_JSONL)
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写回")
    args = parser.parse_args()
    if not args.skus_jsonl.is_file():
        print(f"缺少 {args.skus_jsonl}", file=sys.stderr)
        return 1
    return backfill(args.skus_jsonl, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
