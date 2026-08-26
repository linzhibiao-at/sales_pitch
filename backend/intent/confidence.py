"""置信度打分：根据已提取 slots 评估是否需要 LLM fallback。"""

from __future__ import annotations

from typing import Any


_MUST_HAVE_WEIGHTS: dict[str, float] = {
    "gender": 0.20,
    "season": 0.15,
    "category": 0.15,
}

_IMPORTANT_WEIGHTS: dict[str, float] = {
    "anchor_role": 0.15,
    "target_role": 0.10,
    "style_tags": 0.05,
}

_AUXILIARY_WEIGHTS: dict[str, float] = {
    "occasion_tags": 0.05,
    "color": 0.05,
    "color_series": 0.05,
}

_ALL_SLOT_WEIGHTS = {**_MUST_HAVE_WEIGHTS, **_IMPORTANT_WEIGHTS, **_AUXILIARY_WEIGHTS}


def compute_confidence(
    slots: dict[str, list[str]],
    has_image: bool = False,
    *,
    image_slots: dict[str, list[str]] | None = None,
    anchor_role: str | None = None,
    target_roles: list[str] | None = None,
) -> tuple[float, dict[str, float]]:
    """计算 slots 提取的置信度分数。

    Returns:
        (total_score, per_slot_scores) — 满分 1.0
    """
    per_slot: dict[str, float] = {}

    for slot, weight in _ALL_SLOT_WEIGHTS.items():
        if slot == "anchor_role":
            per_slot[slot] = weight if anchor_role else 0.0
        elif slot == "target_role":
            per_slot[slot] = weight if target_roles else 0.0
        else:
            text_val = slots.get(slot, [])
            image_val = (image_slots or {}).get(slot, [])
            if text_val:
                per_slot[slot] = weight
            elif image_val:
                per_slot[slot] = weight * 0.7
            else:
                per_slot[slot] = 0.0

    if image_slots:
        for key in set(slots.keys()) & set(image_slots.keys()):
            text_val = slots.get(key, [])
            img_val = image_slots.get(key, [])
            if text_val and img_val and text_val != img_val:
                per_slot[key] = per_slot.get(key, 0.0) * 0.5

    if has_image:
        per_slot["_image_bonus"] = 0.10

    total = min(sum(per_slot.values()), 1.0)
    return total, per_slot


def compute_confidence_legacy(
    slots: dict[str, list[str]],
    has_image: bool = False,
) -> float:
    """兼容旧接口：仅返回总分。"""
    total, _ = compute_confidence(slots, has_image=has_image)
    return total
