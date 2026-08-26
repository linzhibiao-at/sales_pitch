"""响应卡片字段编排。"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.models import normalize_gender_first
from backend.recall_paths_config import recall_source_label, show_outfit_rank_scores

logger = logging.getLogger(__name__)


def _item_sku_id(item: Dict[str, Any]) -> str:
    """从 item 中提取 SKU ID（兼容 sku_id / skuId / attrAlias / idAlias）。"""
    raw = (
        item.get("sku_id")
        or item.get("skuId")
        or item.get("attrAlias")
        or item.get("idAlias")
    )
    return str(raw).strip() if raw is not None else ""


def sku_card(row: Dict[str, Any]) -> Dict[str, Any]:
    iq = row.get("image_quality") or {}
    return {
        "sku_id": row.get("sku_id"),
        "spu_id": row.get("spu_id"),
        "title": row.get("title"),
        "role": row.get("role"),
        "gender": normalize_gender_first(row.get("gender")),
        "group_brand": row.get("group_brand"),
        "price": row.get("price"),
        "display_image": row.get("display_image"),
        "tryon_image": row.get("tryon_image"),
        "is_tryon_ready": bool(iq.get("is_tryon_ready")),
        "source_relation_ids": row.get("_source_relation_ids", []),
        "reason": row.get("reason") or "",
    }


def outfit_card(
    outfit: Dict[str, Any],
    *,
    source_relation_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    oid = str(outfit.get("outfit_id") or "")
    recall_src = (
        outfit.get("source")
        or outfit.get("_recall_path")
        or ""
    )
    is_synthetic = oid.startswith("synth_") or recall_src in (
        "text_vector_compose",
        "OUTFIT_TEXT_VECTOR_COMPOSE",
        "query2es_compose",
        "OUTFIT_QUERY2ES_COMPOSE",
        "complementary_model_compose",
        "OUTFIT_COMPLEMENTARY_MODEL",
    )
    show_rank = show_outfit_rank_scores()
    card: Dict[str, Any] = {
        "outfit_id": outfit.get("outfit_id"),
        "id_match": outfit.get("idMatch") or outfit.get("id_match"),
        "name": outfit.get("name"),
        "recall_source": recall_src,
        "recall_source_label": recall_source_label(recall_src),
        "is_synthetic": is_synthetic,
        "master_sku_id": outfit.get("master_sku_id"),
        "master_spu_id": outfit.get("master_spu_id"),
        "display_image": outfit.get("display_image"),
        "index_images": outfit.get("index_images") or [],
        "outfit_tryon_image": outfit.get("outfit_tryon_image") or "",
        "background_img": outfit.get("background_img"),
        "price_total": outfit.get("price_total"),
        "outfit_completeness_score": outfit.get(
            "outfit_completeness_score",
        ),
        "tryon_coverage": outfit.get("tryon_coverage"),
        "reason": outfit.get("reason") or "",
        "items": [
            {
                "sku_id": _item_sku_id(it),
                "spu_id": it.get("spu_id"),
                "role": it.get("role"),
                "title": it.get("title"),
                "price": it.get("price"),
                "display_image": it.get("display_image"),
                "tryon_image": it.get("tryon_image"),
                "is_master": bool(it.get("is_master")),
                "is_anchor": bool(it.get("is_anchor")),
                "reason": it.get("reason") or "",
            }
            for it in (outfit.get("items") or [])
        ],
        "source_outfit_ids": [str(outfit.get("outfit_id") or "")],
        "source_relation_ids": (
            list(source_relation_ids)
            if source_relation_ids is not None
            else list(outfit.get("source_relation_ids") or [])
        ),
    }
    if show_rank:
        card["rank_score"] = outfit.get("_rank_score")
        card["rank_order"] = outfit.get("_rank_order")
        card["rank_score_breakdown"] = outfit.get("_rank_breakdown") or {}
    # 诊断：anchor_graph 搭配中 anchor item 的 sku_id 是否正确
    if recall_src in ("anchor_graph", "OUTFIT_ANCHOR_GRAPH"):
        for it_card in card["items"]:
            if it_card.get("is_anchor") or it_card.get("is_master"):
                logger.debug(
                    "[card_builder] anchor_graph outfit=%s, "
                    "master/anchor item sku_id=%s, spu_id=%s",
                    card.get("outfit_id"),
                    it_card.get("sku_id"),
                    it_card.get("spu_id"),
                )
    return card
