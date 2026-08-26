"""搭配文档色系标签：从单品 color / color_series 提取。"""

from __future__ import annotations

from typing import Any

from backend.intent.color_series_mapper import map_color_to_series_list

COLOR_SERIES_TAB_ORDER = (
    "黑色系",
    "白色系",
    "灰色系",
    "米色系",
    "红色系",
    "粉色系",
    "橙色系",
    "黄色系",
    "绿色系",
    "蓝色系",
    "紫色系",
    "棕色系",
    "多色系",
)


def item_color_series(item: dict[str, Any]) -> list[str]:
    """从搭配单品提取色系列表（多色 SKU 可能返回多个色系）。"""
    cs = item.get("color_series")
    if isinstance(cs, list):
        vals = [str(x).strip() for x in cs if str(x).strip()]
        if vals:
            return vals
    elif isinstance(cs, str) and cs.strip():
        return [cs.strip()]
    # 回退：从 color 派生
    color = item.get("color") or {}
    if isinstance(color, str):
        name = color.strip()
        if name:
            return map_color_to_series_list(name)
        return []
    if isinstance(color, dict):
        name = str(
            color.get("attrName")
            or color.get("colorName")
            or color.get("name")
            or "",
        ).strip()
        if name:
            return map_color_to_series_list(name)
    return []


def outfit_color_series_tags(outfit: dict[str, Any]) -> list[str]:
    """搭配包含的色系列表（去重、有序）。"""
    tags = outfit.get("color_series_tags")
    if isinstance(tags, list) and tags:
        return [str(t).strip() for t in tags if str(t).strip()]
    found: list[str] = []
    seen: set[str] = set()
    for item in outfit.get("items") or []:
        if not isinstance(item, dict):
            continue
        for cs in item_color_series(item):
            if cs and cs not in seen:
                seen.add(cs)
                found.append(cs)
    return found


def outfit_has_color_series(
    outfit: dict[str, Any],
    color_series: str,
) -> bool:
    cs = (color_series or "").strip()
    if not cs:
        return True
    return cs in outfit_color_series_tags(outfit)


def sort_color_series_counts(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    order = {
        name: idx for idx, name in enumerate(COLOR_SERIES_TAB_ORDER)
    }
    return sorted(
        rows,
        key=lambda row: (
            order.get(str(row.get("color_series") or ""), 999),
            str(row.get("color_series") or ""),
        ),
    )
