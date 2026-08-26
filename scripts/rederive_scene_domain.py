#!/usr/bin/env python3
"""对 data/processed/skus.jsonl 重新计算 scene_domain 字段（覆写）。

用途：scene_domain 拆分（运动侧按项目细分 + 236xxx 码解码）后，把新映射
应用到存量 SKU。复用 backend.intent.sku_attributes.extract_scene_domain，
与 enrich_sku_attributes / get_attr 同源，保证召回期实时推导与落盘值一致。

只覆写 scene_domain 一个字段，其它字段不动；不重 embed，后续由
migrate_milvus_add_attr_fields.py / build_fila_es_index.py 把新值回写索引。

用法：
  python scripts/rederive_scene_domain.py
  python scripts/rederive_scene_domain.py --dry-run     # 只打印分布，不落盘
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

from backend.intent.sku_attributes import extract_scene_domain

SKUS_PATH = ROOT / "data" / "processed" / "skus.jsonl"


def main() -> int:
    parser = argparse.ArgumentParser(description="重算 skus.jsonl 的 scene_domain")
    parser.add_argument("--dry-run", action="store_true", help="只打印新旧分布对比，不落盘")
    args = parser.parse_args()

    before = Counter()
    after = Counter()
    changed_from_to: Counter = Counter()  # (old, new) → n
    rows: list[str] = []
    total = 0

    with SKUS_PATH.open(encoding="utf-8") as fin:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            sku = json.loads(line)
            total += 1
            old = sku.get("scene_domain") or ""
            before[old] += 1
            new = extract_scene_domain(
                sku.get("category_l1") or "",
                sku.get("category_l2") or "",
                sku.get("role") or "",
                sku.get("occasion_tags") or "",
                sku.get("title") or "",
                sku.get("search_keywords") or "",
                sku.get("series") or "",
                sku.get("sub_series") or "",
            )
            after[new] += 1
            if new != old:
                changed_from_to[(old, new)] += 1
            sku["scene_domain"] = new
            rows.append(json.dumps(sku, ensure_ascii=False))

    def _print_dist(c: Counter, label: str) -> None:
        print(f"\n{label} (共 {sum(c.values())} 条):")
        for k, v in c.most_common():
            print(f"  {v:6d}  {k!r}")

    _print_dist(before, "=== scene_domain 旧分布 ===")
    _print_dist(after, "=== scene_domain 新分布 ===")
    print(f"\n=== 迁移: {sum(changed_from_to.values())}/{total} 条变化 ===")
    for (o, n), v in changed_from_to.most_common(40):
        print(f"  {v:5d}  {o!r:12s} → {n!r}")

    if args.dry_run:
        print("\n[--dry-run] 未落盘。")
        return 0

    tmp = SKUS_PATH.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fout:
        for r in rows:
            fout.write(r + "\n")
    tmp.replace(SKUS_PATH)
    print(f"\n已覆写 {SKUS_PATH} ({total} 条)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
