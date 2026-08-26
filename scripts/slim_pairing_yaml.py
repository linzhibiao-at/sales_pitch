#!/usr/bin/env python3
"""精简 pairing YAML：删除代码不读取的冗余字段，缩短 key 名。

改前（co-occurrence，单个锚点 ~40 行）：
  短袖T恤:
    role: 上装
    anchor_as_master_count: 1234
    required_companion_roles: [上装]
    primary_companions: [梭织长裤, ...]
    allowed_companions: [梭织短裤, ...]
    forbidden_companions: [儿童鞋, ...]          ← 不读取
    companions:                                   ← 不读取，~30 行统计
      - {category_l2: 梭织长裤, as_companion_count: 100, ...}
      ...

改后（~5 行）：
  短袖T恤:
    role: 上装
    anchor_count: 1234
    primary: [梭织长裤, 针织长裤, 板鞋]
    allowed: [梭织短裤, 半身裙, 帆布鞋]

同时合并 category_meta 到 pairing_rules（role 内联），消除独立 block。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COOC_PATH = ROOT / "backend" / "intent" / "dictionaries" / "category_l2_pairing.yaml"
CART_PATH = ROOT / "backend" / "intent" / "dictionaries" / "category_l2_cartesian_pairing.yaml"
MERGE_PATH = ROOT / "backend" / "intent" / "dictionaries" / "category_l2_merge.yaml"
EXCL_PATH = ROOT / "backend" / "intent" / "dictionaries" / "non_clothing_exclusion.yaml"

with MERGE_PATH.open(encoding="utf-8") as f:
    _merge_map = yaml.safe_load(f) or {}
with EXCL_PATH.open(encoding="utf-8") as f:
    _excl_data = yaml.safe_load(f) or {}
_excluded = set(_excl_data.get("non_clothing", []) + _excl_data.get("intimate_swimwear", []))


def _norm(cat: str) -> str:
    return _merge_map.get(cat, cat)


def slim_pairing(path: Path) -> None:
    old_size = path.stat().st_size
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    old_rules = data.get("pairing_rules") or {}
    old_meta = data.get("category_meta") or {}

    # Build slim rules: merge role from category_meta, keep only needed fields
    new_rules: dict[str, dict] = {}
    for cat, rule in old_rules.items():
        if not isinstance(rule, dict):
            continue
        std_cat = _norm(cat)
        if std_cat in _excluded:
            continue
        meta = old_meta.get(cat) or old_meta.get(std_cat) or {}
        slim: dict[str, object] = {"role": meta.get("role") or rule.get("role") or ""}

        # anchor_as_master_count → anchor_count (co-occurrence only)
        anchor_count = rule.get("anchor_as_master_count")
        if anchor_count is not None:
            slim["anchor_count"] = anchor_count

        # primary_companions → primary (normalize names)
        primary = rule.get("primary_companions") or []
        norm_primary = sorted({_norm(c) for c in primary if isinstance(c, str)} - _excluded)
        if norm_primary:
            slim["primary"] = norm_primary

        # allowed_companions → allowed (normalize names)
        allowed = rule.get("allowed_companions") or []
        norm_allowed = sorted({_norm(c) for c in allowed if isinstance(c, str)} - _excluded)
        if norm_allowed:
            slim["allowed"] = norm_allowed

        # Merge if std_cat already exists (from another raw variant)
        if std_cat in new_rules:
            existing = new_rules[std_cat]
            if not existing.get("role") and slim.get("role"):
                existing["role"] = slim["role"]
            if "primary" not in existing and "primary" in slim:
                existing["primary"] = slim["primary"]
            if "allowed" not in existing and "allowed" in slim:
                existing["allowed"] = slim["allowed"]
            if "anchor_count" not in existing and "anchor_count" in slim:
                existing["anchor_count"] = slim["anchor_count"]
        else:
            new_rules[std_cat] = slim

    # Write slim YAML
    out = {"pairing_rules": new_rules}
    with path.open("w", encoding="utf-8") as f:
        f.write("# 中类(category_l2)搭配规则\n")
        f.write(f"# 共 {len(new_rules)} 个中类\n")
        f.write("# 字段: role(角色) / anchor_count(锚点统计数) / primary(首选搭配) / allowed(允许搭配)\n")
        f.write("# 生成脚本: scripts/extract_category_l2_pairing_rules.py 或 scripts/gen_category_l2_cartesian_pairing.py\n\n")
        yaml.dump(out, f, allow_unicode=True, default_flow_style=False, sort_keys=True)

    new_size = path.stat().st_size
    print(f"  {path.name}: {len(old_rules)} → {len(new_rules)} rules, {old_size} → {new_size} bytes ({new_size*100//old_size}%)")


print("=== Slimming pairing YAMLs ===")
slim_pairing(COOC_PATH)
slim_pairing(CART_PATH)

# Verify match rate
cats_in_skus = set()
with (ROOT / "data" / "processed" / "skus.jsonl").open(encoding="utf-8") as f:
    for line in f:
        r = json.loads(line)
        c = r.get("category_l2", "")
        if c and not r.get("excluded_from_pairing"):
            cats_in_skus.add(c)

for name, path in [("co-occurrence", COOC_PATH), ("cartesian", CART_PATH)]:
    with path.open(encoding="utf-8") as f:
        d = yaml.safe_load(f) or {}
    keys = set((d.get("pairing_rules") or {}).keys())
    match = len(cats_in_skus & keys)
    print(f"  {name}: {match}/{len(cats_in_skus)} = {match*100//len(cats_in_skus)}%")

print("Done.")
