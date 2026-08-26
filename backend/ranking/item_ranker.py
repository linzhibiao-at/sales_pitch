"""单品补全排序。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from backend.ranking.scoring import intent_attr_match, price_match


def _sku_diversity_score(
    row: Dict[str, Any],
    seen_spu: Dict[str, int],
    seen_cat_color: Dict[tuple, int],
) -> float:
    spu_id = str(row.get("spu_id") or "")
    cat = str(row.get("category_l2") or "").strip()
    clr = row.get("color_series") or []
    if isinstance(clr, str):
        clr = [clr] if clr else []

    spu_count = seen_spu.get(spu_id, 0)
    cat_clr_count = seen_cat_color.get((cat, tuple(clr)), 0) if cat and clr else 0

    spu_penalty = max(0.0, 1.0 - 0.3 * spu_count)
    cat_clr_penalty = max(0.0, 1.0 - 0.25 * cat_clr_count) if cat and clr else 0.5

    return round(0.5 * spu_penalty + 0.5 * cat_clr_penalty, 4)


def rank_skus(
    candidates: List[Dict[str, Any]],
    *,
    anchor_sim: Optional[Dict[str, float]] = None,
    intent_gender: Optional[str] = None,
    intent_season: Optional[List[str]] = None,
    intent_tags: Optional[List[str]] = None,
    budget_max: Optional[float] = None,
    compat_score: Optional[Dict[str, float]] = None,
    outfit_quality: Optional[Dict[str, float]] = None,
    companion_color_series: Optional[List[str]] = None,
) -> List[Tuple[float, Dict[str, Any]]]:
    """单品补全排序。

    ``companion_color_series``：锚点色系的搭配白名单（来自 pairing 规则）。纯色 SKU
    （color_series 长度为 1 且命中白名单）获得 pure_color_bonus，排在多色 SKU 之前。
    """
    sim = anchor_sim or {}
    cs = compat_score or {}
    oq = outfit_quality or {}
    tags = list(intent_tags or [])
    season = list(intent_season or [])
    companion_set = set(companion_color_series or [])
    ranked: List[Tuple[float, Dict[str, Any]]] = []
    seen_spu: dict[str, int] = {}
    seen_cat_color: dict[tuple, int] = {}
    for row in candidates:
        sid = str(row.get("sku_id") or "")
        spu = str(row.get("spu_id") or "")
        if seen_spu.get(spu, 0) >= 2:
            continue
        seen_spu[spu] = seen_spu.get(spu, 0) + 1
        cat = str(row.get("category_l2") or "").strip()
        clr = row.get("color_series") or []
        if isinstance(clr, str):
            clr = [clr] if clr else []
        if cat and clr:
            key = (cat, tuple(clr))
            seen_cat_color[key] = seen_cat_color.get(key, 0) + 1
        cscore = float(cs.get(sid, 0.7))
        asim = float(sim.get(sid, 0.4))
        im = intent_attr_match(row, intent_gender, season, tags)
        oqv = float(oq.get(sid, 0.5))
        tryon = 1.0 if row.get("tryon_image") else 0.2
        price = float(row.get("price") or 0.0)
        pm = price_match(price, budget_max)
        div = _sku_diversity_score(row, seen_spu, seen_cat_color)
        # 纯色 boost：纯色 SKU（长度 1）且命中搭配白名单 → +1，多色 → 0
        pure_bonus = 1.0 if (len(clr) == 1 and clr[0] in companion_set) else 0.0
        score = (
            0.25 * cscore
            + 0.20 * asim
            + 0.15 * im
            + 0.15 * oqv
            + 0.10 * tryon
            + 0.05 * pm
            + 0.05 * div
            + 0.05 * pure_bonus
        )
        ranked.append((score, row))
    ranked.sort(key=lambda x: -x[0])
    return ranked
