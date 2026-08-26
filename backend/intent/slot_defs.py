"""Slot 定义与枚举。"""

from __future__ import annotations

SLOTS = [
    "anchor_role",
    "target_role",
    "season",
    "occasion_tags",
    "style_tags",
    "gender",
    "age",
    "category",
    "color",
    "modeling",
    "budget_min",
    "budget_max",
]

# 完整搭配角色定义（外套归入上装，包归入配饰）
FULL_OUTFIT_ROLES = ["上装", "下装", "鞋", "配饰"]
REQUIRED_ROLES = ["上装", "下装", "鞋"]
OPTIONAL_ROLES = ["配饰", "连衣裙"]

# 默认 target_roles（无明确指定时）
DEFAULT_TARGET_ROLES = ["上装", "下装", "鞋", "配饰"]

# role 中文 → 英文映射
ROLE_ZH_TO_EN: dict[str, str] = {
    "上装": "top",
    "下装": "bottoms",
    "鞋": "shoes",
    "配饰": "accessory",
    "连衣裙": "dress",
}

ROLE_EN_TO_ZH: dict[str, str] = {v: k for k, v in ROLE_ZH_TO_EN.items()}


def normalize_role(val: str | None) -> str:
    """统一角色 token 为英文（top/bottoms/shoes/accessory/dress）。

    规则文件与冲突检测用英文 token，但 ES 固定搭配库的 item.role 多为中文
    （上装/下装/鞋/配饰/连衣裙）。此处做归一化，使两边可比较。
    已是英文或未识别的值原样小写返回。
    """
    if not val:
        return ""
    v = str(val).strip()
    mapped = ROLE_ZH_TO_EN.get(v)
    if mapped:
        return mapped
    return v.lower()

ROLE_EN_TO_SEARCH_KEYWORD: dict[str, str] = {
    "top": "上衣",
    "bottoms": "下装",
    "shoes": "鞋",
    "dress": "连衣裙",
    "accessory": "配饰",
}

# 搭配内单品展示顺序优先级（越小越靠前）。
# 全部单品按 top > bottoms/dress > shoes > accessory 排列，未知/空角色垫底。
# bottoms 与 dress 同级（连衣裙即一身装，视同下装位；与下装互斥不共存）。
# 不再把输入商品强制放首位——展示顺序完全由 role 决定。
ROLE_DISPLAY_PRIORITY: dict[str, int] = {
    "top": 1,
    "bottoms": 2,
    "dress": 2,
    "shoes": 3,
    "accessory": 4,
}


def role_display_priority(role: str | None) -> int:
    """返回 role 的展示优先级，未知/空角色返回 99 垫底。"""
    return ROLE_DISPLAY_PRIORITY.get(normalize_role(role), 99)
