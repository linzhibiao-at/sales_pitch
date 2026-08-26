#!/usr/bin/env python3
"""为 cartesian pairing YAML 补齐缺失中类的默认规则。

对缺失中类按 role 生成保守的搭配规则：
  - 配饰类：可与所有非配饰类搭配
  - 外套类：可与下装、鞋、配饰搭配
  - 连衣裙/背带裙类：可与鞋、配饰搭配
  - 两件套类：可与鞋、配饰搭配
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CART_PATH = ROOT / "backend" / "intent" / "dictionaries" / "category_l2_cartesian_pairing.yaml"
SKUS_PATH = ROOT / "data" / "processed" / "skus.jsonl"

with CART_PATH.open(encoding="utf-8") as f:
    cart_data = yaml.safe_load(f) or {}
cart_rules = cart_data.get("pairing_rules") or {}

# Load all clothing categories from skus.jsonl, grouped by role
cats_by_role: dict[str, set[str]] = {}
all_clothing_cats = set()
with SKUS_PATH.open(encoding="utf-8") as f:
    for line in f:
        r = json.loads(line)
        c = r.get("category_l2", "")
        role = r.get("role", "")
        if c and not r.get("excluded_from_pairing"):
            all_clothing_cats.add(c)
            cats_by_role.setdefault(role, set()).add(c)

# Find missing categories
missing = sorted(all_clothing_cats - set(cart_rules.keys()))
print(f"Missing from cartesian: {len(missing)}")
for c in missing:
    print(f"  {c}")

# Role mapping for missing categories
MISSING_ROLE_MAP = {
    "手套": "accessory",
    "护具": "accessory",
    "时装帽": "accessory",
    "渔夫帽": "accessory",
    "腰带": "accessory",
    "棉服": "top",
    "毛呢上衣": "top",
    "背带裙": "dress",
    "梭织两件套": "top",
    "针织两件套": "top",
}

# For each role, what roles can pair with it
ROLE_PAIRS = {
    "top": {"bottoms", "shoes", "accessory"},
    "bottoms": {"top", "shoes", "accessory"},
    "shoes": {"top", "bottoms", "accessory"},
    "accessory": {"top", "bottoms", "shoes", "dress"},
    "dress": {"shoes", "accessory"},
}

for cat in missing:
    role = MISSING_ROLE_MAP.get(cat, "accessory")
    companion_roles = ROLE_PAIRS.get(role, {"top", "bottoms", "shoes"})
    
    # All categories from companion roles
    companions = set()
    for cr in companion_roles:
        companions |= cats_by_role.get(cr, set())
    companions.discard(cat)
    
    cart_rules[cat] = {
        "role": role,
        "primary": sorted(companions),
        "allowed": sorted(companions),
    }
    print(f"  Added: {cat} (role={role}, {len(companions)} companions)")

cart_data["pairing_rules"] = cart_rules
with CART_PATH.open("w", encoding="utf-8") as f:
    f.write("# 中类(category_l2)搭配规则\n")
    f.write(f"# 共 {len(cart_rules)} 个中类\n")
    f.write("# 字段: role(角色) / primary(首选搭配) / allowed(允许搭配)\n")
    f.write("# 生成脚本: scripts/gen_category_l2_cartesian_pairing.py + scripts/fill_missing_pairing.py\n\n")
    yaml.dump(cart_data, f, allow_unicode=True, default_flow_style=False, sort_keys=True)

# Verify
cart_keys = set(cart_rules.keys())
match = len(all_clothing_cats & cart_keys)
total = len(all_clothing_cats)
print(f"\nMatch rate: {match}/{total} = {match*100//total}%")
still_missing = sorted(all_clothing_cats - cart_keys)
if still_missing:
    print(f"Still missing: {still_missing}")
else:
    print("100% match achieved!")
