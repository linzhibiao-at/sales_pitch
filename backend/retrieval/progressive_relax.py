"""渐进式过滤放宽驱动：0 命中时按优先级逐个丢弃 soft slot 直到命中数达标。

驱动器本身与具体通路无关：每条召回通路提供自己的 ``search_fn(dropped_set)``
闭包，将 dropped 的 slot 名映射为该通路的 skip-knob / 重建的 expr 片段。

硬约束（gender/season/age）永不出现在 ``relax_priority`` 中，故循环在耗尽
soft 链后自然停在身份墙前——即使角色仍为空也不会跨性别/跨季节召回。
"""
from __future__ import annotations

from typing import Callable, TypeVar

T = TypeVar("T")


def run_with_progressive_relax(
    search_fn: Callable[[set[str]], list[T]],
    priority: list[str],
    min_hits: int,
) -> tuple[list[T], list[str]]:
    """按 ``priority`` 顺序逐个丢弃 slot 并重跑 ``search_fn``，直到命中数 ≥ ``min_hits``。

    - 首先用空 dropped 集跑一次（不退化：非空即立即返回）。
    - ``dropped`` 为实际被牺牲的 slot 名有序列表（可观测性）。
    - 耗尽 ``priority`` 仍不达标 → 返回最后一次（可能为空）的结果。
    - 硬墙隐式：硬 slot 不在 ``priority`` 中，循环不会触及。
    """
    dropped: list[str] = []
    hits = search_fn(set())
    for slot in priority:
        if len(hits) >= min_hits:
            return hits, dropped
        dropped.append(slot)
        hits = search_fn(set(dropped))
    return hits, dropped


def get_relax_config() -> tuple[bool, list[str], int]:
    """读取 ``recommend.{enable_progressive_relax,relax_priority,relax_min_hits}``。

    - ``enable_progressive_relax`` 缺省 True。
    - ``relax_priority`` 缺省 ``[modeling, length_class, coverage, series,
      scene_domain, color_series, category_l2, anchor_attr_must_not, up_time, price]``。
    - ``relax_min_hits`` 缺省 1（即 0 命中触发）。
    """
    from backend.config import load_config

    data = load_config() or {}
    rec = data.get("recommend") or {}
    enabled = bool(rec.get("enable_progressive_relax", True))
    priority = list(rec.get("relax_priority") or [
        "modeling", "length_class", "coverage", "series", "scene_domain",
        "color_series", "category_l2", "anchor_attr_must_not", "up_time", "price",
    ])
    try:
        min_hits = int(rec.get("relax_min_hits", 1))
    except (TypeError, ValueError):
        min_hits = 1
    return enabled, priority, min_hits
