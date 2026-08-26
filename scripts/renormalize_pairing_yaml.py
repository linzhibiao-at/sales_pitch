#!/usr/bin/env python3
"""对现有 pairing YAML 重新归一化 key：合并同义中类条目。"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MERGE_PATH = ROOT / "backend" / "intent" / "dictionaries" / "category_l2_merge.yaml"
EXCL_PATH = ROOT / "backend" / "intent" / "dictionaries" / "non_clothing_exclusion.yaml"
CART_PATH = ROOT / "backend" / "intent" / "dictionaries" / "category_l2_cartesian_pairing.yaml"
COOC_PATH = ROOT / "backend" / "intent" / "dictionaries" / "category_l2_pairing.yaml"

with MERGE_PATH.open(encoding="utf-8") as f:
    merge_map = yaml.safe_load(f) or {}
with EXCL_PATH.open(encoding="utf-8") as f:
    excl_data = yaml.safe_load(f) or {}
excluded = set(excl_data.get("non_clothing", []) + excl_data.get("intimate_swimwear", []))

def norm(cat: str) -> str:
    return merge_map.get(cat, cat)

def norm_str_list(items):
    """归一化列表（元素可能是 str 或 dict），去重 + 排除。"""
    result = set()
    for item in items or []:
        if isinstance(item, str):
            n = norm(item)
            if n not in excluded:
                result.add(n)
        elif isinstance(item, dict):
            n = norm(item.get("category_l2", ""))
            if n and n not in excluded:
                result.add(n)
    return sorted(result)

# ── 1. Cartesian pairing YAML ──
print("=== Re-normalizing cartesian_pairing.yaml ===")
with CART_PATH.open(encoding="utf-8") as f:
    cart_data = yaml.safe_load(f) or {}
cart_rules = cart_data.get("pairing_rules") or {}
print(f"  Before: {len(cart_rules)} keys")

new_cart: dict[str, dict] = {}
for raw_key, rule in cart_rules.items():
    std_key = norm(raw_key)
    if std_key in excluded:
        continue

    norm_primary = norm_str_list(rule.get("primary_companions"))
    norm_allowed = norm_str_list(rule.get("allowed_companions"))
    norm_forbidden = norm_str_list(rule.get("forbidden_companions"))

    norm_detail = []
    for d in (rule.get("companions_detail") or []):
        if not isinstance(d, dict):
            continue
        cat = norm(d.get("category_l2", ""))
        if cat in excluded:
            continue
        d2 = dict(d)
        d2["category_l2"] = cat
        norm_detail.append(d2)

    if std_key in new_cart:
        existing = new_cart[std_key]
        existing["primary_companions"] = sorted(set(existing.get("primary_companions") or []) | set(norm_primary))
        existing["allowed_companions"] = sorted(set(existing.get("allowed_companions") or []) | set(norm_allowed))
        existing["forbidden_companions"] = sorted(set(existing.get("forbidden_companions") or []) | set(norm_forbidden))
        detail_map = {}
        for d in (existing.get("companions_detail") or []) + norm_detail:
            cat = d.get("category_l2", "")
            old = detail_map.get(cat)
            if old is None or d.get("score", 0) > old.get("score", 0):
                detail_map[cat] = d
        existing["companions_detail"] = sorted(detail_map.values(), key=lambda x: x.get("score", 0), reverse=True)
    else:
        new_cart[std_key] = {
            "role": rule.get("role", ""),
            "primary_companions": norm_primary,
            "allowed_companions": norm_allowed,
            "forbidden_companions": norm_forbidden,
            "companions_detail": norm_detail,
        }

print(f"  After: {len(new_cart)} keys")
cart_data["pairing_rules"] = new_cart
with CART_PATH.open("w", encoding="utf-8") as f:
    yaml.dump(cart_data, f, allow_unicode=True, default_flow_style=False, sort_keys=True)
print(f"  Written to {CART_PATH.name}")

# ── 2. Check match rate ──
cats_in_skus = set()
with (ROOT / "data" / "processed" / "skus.jsonl").open(encoding="utf-8") as f:
    for line in f:
        r = json.loads(line)
        c = r.get("category_l2", "")
        if c and not r.get("excluded_from_pairing"):
            cats_in_skus.add(c)

cart_keys = set(new_cart.keys())
with COOC_PATH.open(encoding="utf-8") as f:
    cooc_data = yaml.safe_load(f) or {}
cooc_keys = set((cooc_data.get("pairing_rules") or {}).keys())

print(f"\n=== Match rate (clothing cats only: {len(cats_in_skus)}) ===")
print(f"  co-occurrence pairing: {len(cats_in_skus & cooc_keys)}/{len(cats_in_skus)} = {len(cats_in_skus & cooc_keys)*100//len(cats_in_skus)}%")
print(f"  cartesian pairing:     {len(cats_in_skus & cart_keys)}/{len(cats_in_skus)} = {len(cats_in_skus & cart_keys)*100//len(cats_in_skus)}%")
print(f"  either:                {len(cats_in_skus & (cooc_keys | cart_keys))}/{len(cats_in_skus)} = {len(cats_in_skus & (cooc_keys | cart_keys))*100//len(cats_in_skus)}%")

missing_cart = sorted(cats_in_skus - cart_keys)
if missing_cart:
    print(f"\n  Missing from cartesian ({len(missing_cart)}):")
    for c in missing_cart:
        print(f"    {c}")
missing_cooc = sorted(cats_in_skus - cooc_keys)
if missing_cooc:
    print(f"\n  Missing from co-occurrence ({len(missing_cooc)}):")
    for c in missing_cooc:
        print(f"    {c}")
