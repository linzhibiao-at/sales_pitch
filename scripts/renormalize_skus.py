#!/usr/bin/env python3
"""对 data/processed/skus.jsonl 重新应用 category_l2 归一化 + 标记排除类。

用法：
  python scripts/renormalize_skus.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MERGE_PATH = ROOT / "backend" / "intent" / "dictionaries" / "category_l2_merge.yaml"
EXCL_PATH = ROOT / "backend" / "intent" / "dictionaries" / "non_clothing_exclusion.yaml"
SKUS_PATH = ROOT / "data" / "processed" / "skus.jsonl"

with MERGE_PATH.open(encoding="utf-8") as f:
    merge_map = yaml.safe_load(f) or {}
with EXCL_PATH.open(encoding="utf-8") as f:
    excl_data = yaml.safe_load(f) or {}
excluded = set(excl_data.get("non_clothing", []) + excl_data.get("intimate_swimwear", []))

print(f"merge_map: {len(merge_map)} rules")
print(f"excluded: {len(excluded)} categories")

before_cats = Counter()
after_cats = Counter()
excluded_count = 0
total = 0

tmp_path = SKUS_PATH.with_suffix(".jsonl.tmp")
with SKUS_PATH.open(encoding="utf-8") as fin, tmp_path.open("w", encoding="utf-8") as fout:
    for line in fin:
        line = line.strip()
        if not line:
            continue
        sku = json.loads(line)
        total += 1
        raw_cat = sku.get("category_l2", "")
        before_cats[raw_cat] += 1
        # Re-normalize
        normalized = merge_map.get(raw_cat, raw_cat)
        sku["category_l2"] = normalized
        # Mark exclusion
        sku["excluded_from_pairing"] = normalized in excluded
        if sku["excluded_from_pairing"]:
            excluded_count += 1
        after_cats[normalized] += 1
        fout.write(json.dumps(sku, ensure_ascii=False) + "\n")

tmp_path.replace(SKUS_PATH)

print(f"\nProcessed {total} SKUs")
print(f"Excluded from pairing: {excluded_count}")
print(f"Unique category_l2 before: {len(before_cats)}")
print(f"Unique category_l2 after:  {len(after_cats)}")

# Show merged examples
merged = [(raw, std) for raw, std in merge_map.items() if raw in before_cats]
print(f"\nMerged {len(merged)} raw → standard names:")
for raw, std in sorted(merged):
    print(f"  {raw} → {std}  ({before_cats[raw]} SKUs)")
