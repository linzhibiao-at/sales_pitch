"""召回分支枚举：用于 debug 日志与 replay 归因。"""

from __future__ import annotations

from enum import Enum


class RecallPathway(str, Enum):
    """不同召回通路的稳定枚举标签（值即 JSON 中的 code）。"""

    # 对话锚点解析
    ANCHOR_EXPLICIT = "ANCHOR_EXPLICIT"
    ANCHOR_SKU_VECTOR = "ANCHOR_SKU_VECTOR"
    ANCHOR_SKU_TEXT_VECTOR = "ANCHOR_SKU_TEXT_VECTOR"
    ANCHOR_SKU_VECTOR_FUSED = "ANCHOR_SKU_VECTOR_FUSED"
    ANCHOR_NONE = "ANCHOR_NONE"

    # 套装召回
    OUTFIT_ANCHOR_GRAPH = "OUTFIT_ANCHOR_GRAPH"
    OUTFIT_TEXT_VECTOR_COMPOSE = "OUTFIT_TEXT_VECTOR_COMPOSE"
    OUTFIT_QUERY2ES_COMPOSE = "OUTFIT_QUERY2ES_COMPOSE"
    OUTFIT_DUAL_MERGED = "OUTFIT_DUAL_MERGED"
    OUTFIT_GLOBAL_COMPOSE = "OUTFIT_GLOBAL_COMPOSE"
    OUTFIT_SKU_TO_OUTFITS = "OUTFIT_SKU_TO_OUTFITS"
    OUTFIT_TEXT_ES = "OUTFIT_TEXT_ES"
    OUTFIT_TEXT_ES_PLUS_VECTOR = "OUTFIT_TEXT_ES_PLUS_VECTOR"
    OUTFIT_SKU_TEXT_VECTOR = "OUTFIT_SKU_TEXT_VECTOR"

    # 互补 SKU（关系表）
    SKU_RELATION_COMPAT = "SKU_RELATION_COMPAT"
    SKU_EMPTY_NO_ANCHOR = "SKU_EMPTY_NO_ANCHOR"
    SKU_SKIPPED_NO_ANCHOR = "SKU_SKIPPED_NO_ANCHOR"

    # 多模态互补模型
    SKU_COMPLEMENTARY_MODEL = "SKU_COMPLEMENTARY_MODEL"
    OUTFIT_COMPLEMENTARY_MODEL = "OUTFIT_COMPLEMENTARY_MODEL"


RECALL_PATHWAY_LABELS: dict[RecallPathway, str] = {
    RecallPathway.ANCHOR_EXPLICIT: "锚点-显式/文案解析",
    RecallPathway.ANCHOR_SKU_VECTOR: "锚点-SKU 图文向量召回",
    RecallPathway.ANCHOR_SKU_TEXT_VECTOR: "锚点-SKU 文本向量召回",
    RecallPathway.ANCHOR_SKU_VECTOR_FUSED: "锚点-SKU 图文+文本向量融合",
    RecallPathway.ANCHOR_NONE: "锚点-无",
    RecallPathway.OUTFIT_ANCHOR_GRAPH: "套装-相似固定搭配",
    RecallPathway.OUTFIT_TEXT_VECTOR_COMPOSE: "套装-文本向量临时拼套",
    RecallPathway.OUTFIT_QUERY2ES_COMPOSE: "套装-Query2ES 意图拼套",
    RecallPathway.OUTFIT_DUAL_MERGED: "套装-双路合并排序",
    RecallPathway.OUTFIT_GLOBAL_COMPOSE: "套装-全局候选池拼套",
    RecallPathway.OUTFIT_SKU_TO_OUTFITS: "套装-sku_to_outfits 图召回",
    RecallPathway.OUTFIT_TEXT_ES: "套装-文本/本地检索",
    RecallPathway.OUTFIT_TEXT_ES_PLUS_VECTOR: "套装-文本+搭配向量融合",
    RecallPathway.OUTFIT_SKU_TEXT_VECTOR: "套装-SKU文本向量扩展",
    RecallPathway.SKU_RELATION_COMPAT: "单品-锚点关系兼容召回",
    RecallPathway.SKU_EMPTY_NO_ANCHOR: "单品-无锚点空结果",
    RecallPathway.SKU_SKIPPED_NO_ANCHOR: "单品-无锚点跳过",
    RecallPathway.SKU_COMPLEMENTARY_MODEL: "单品-多模态互补模型召回",
    RecallPathway.OUTFIT_COMPLEMENTARY_MODEL: "套装-多模态互补模型拼套",
}


def pathway_log_fields(pathway: RecallPathway) -> dict[str, str]:
    """生成写入 recommend_stage / log_flow 的字段。"""
    return {
        "recall_pathway": pathway.value,
        "召回通路": RECALL_PATHWAY_LABELS[pathway],
    }


def chat_recall_pathway_bundle(
    anchor: RecallPathway,
    outfit: RecallPathway,
    sku: RecallPathway,
) -> dict[str, str]:
    """对话管线汇总：一条日志里区分锚点 / 套装 / 单品三条通路。"""
    return {
        "anchor_pathway": anchor.value,
        "outfit_pathway": outfit.value,
        "sku_pathway": sku.value,
        "锚点通路": RECALL_PATHWAY_LABELS[anchor],
        "套装通路": RECALL_PATHWAY_LABELS[outfit],
        "单品通路": RECALL_PATHWAY_LABELS[sku],
    }
