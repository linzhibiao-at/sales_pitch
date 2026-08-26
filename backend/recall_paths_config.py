"""搭配召回通路开关（config.yaml recommend.recall_paths）。"""

from __future__ import annotations

from typing import Any

from backend.config import load_config

# 多路召回开关 key → 调试台展示名
RECALL_PATH_SWITCH_LABELS: dict[str, str] = {
    "image_vector": "相似固定搭配",
    "text_vector": "文本向量",
    "query2es": "Query2ES",
    "complementary_model": "互补模型",
}

# 搭配 source / _recall_path → 卡片展示名
RECALL_SOURCE_LABELS: dict[str, str] = {
    "anchor_graph": "相似固定搭配",
    "OUTFIT_ANCHOR_GRAPH": "相似固定搭配",
    "image_vector": "相似固定搭配",
    "text_vector_compose": "文本向量",
    "OUTFIT_TEXT_VECTOR_COMPOSE": "文本向量",
    "query2es_compose": "Query2ES",
    "OUTFIT_QUERY2ES_COMPOSE": "Query2ES",
    "complementary_model_compose": "互补模型",
    "OUTFIT_COMPLEMENTARY_MODEL": "互补模型",
}


def recall_source_label(source: str) -> str:
    """将内部 recall source 转为前端展示文案。"""
    key = str(source or "").strip()
    if not key:
        return ""
    return RECALL_SOURCE_LABELS.get(key, key)


def get_outfit_recall_path_switches() -> dict[str, bool]:
    """返回 image_vector / query2es / text_vector 是否启用。"""
    rec = load_config().get("recommend") or {}
    paths = rec.get("recall_paths") or {}
    if not isinstance(paths, dict):
        paths = {}
    return {
        "image_vector": bool(paths.get("image_vector", True)),
        "query2es": bool(paths.get("query2es", True)),
        "text_vector": bool(paths.get("text_vector", True)),
        "complementary_model": bool(paths.get("complementary_model", False)),
    }


def es_top_n_per_role() -> int:
    rec = load_config().get("recommend") or {}
    n = int(rec.get("es_top_n_per_role") or 0)
    if n > 0:
        return n
    return int(rec.get("default_sku_per_role") or 3)


def show_outfit_rank_scores() -> bool:
    """是否在 API 卡片与调试台展示搭配排序得分。"""
    rec = load_config().get("recommend") or {}
    return bool(rec.get("show_outfit_rank_scores", False))


def tryon_enabled() -> bool:
    """虚拟试穿是否默认启用。"""
    rec = load_config().get("recommend") or {}
    tryon = rec.get("tryon") or {}
    return bool(tryon.get("enabled", False))


def get_ui_config() -> dict[str, Any]:
    """前端调试台可读取的展示开关。"""
    cfg = load_config()
    ui_mode = str(cfg.get("ui_mode") or "debug")
    is_presentation = ui_mode == "presentation"

    models_cfg = cfg.get("models") or {}
    # 取第一个 section 的 model 作为默认值
    default_model = ""
    for section in ("intent_llm", "vision_llm", "reason_llm", "ranking_llm"):
        m = (models_cfg.get(section) or {}).get("model")
        if m:
            default_model = str(m)
            break
    available_models = [
        "qwen3.5-flash",
        "qwen3.6-flash",
        "qwen3.6-plus",
        "qwen3.7-plus",
        "claude-sonnet-4-6",
        "google/gemini-3.1-pro-preview",
    ]
    result = {
        "ui_mode": ui_mode,
        "show_outfit_rank_scores": show_outfit_rank_scores(),
        "tryon_enabled": tryon_enabled(),
        "recall_path_labels": dict(RECALL_PATH_SWITCH_LABELS),
        "recall_source_labels": dict(RECALL_SOURCE_LABELS),
        "available_models": available_models,
        "default_model": default_model,
    }

    if is_presentation:
        # 对外展示：固定模型、关闭排序得分与试穿、强制开启 LLM 排序+理由
        result["default_model"] = "qwen3.5-flash"
        result["default_ranking_model"] = "qwen3.5-flash"
        result["show_outfit_rank_scores"] = False
        result["tryon_enabled"] = False
        result["enable_llm_rank_reason"] = True

    return result
