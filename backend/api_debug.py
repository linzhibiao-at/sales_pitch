"""HTTP / 流程调试日志与推荐阶段耗时。"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

from backend.config import load_config
from backend.logging_config import ReadableFormatter

_logger = logging.getLogger("fila_agent.api_io")
_logger.setLevel(logging.INFO)

# Uvicorn 默认 logging 配置不为 root 挂 StreamHandler；root 的 lastResort
# 仅 WARNING+，导致本模块的 INFO 阶段日志在控制台不可见。
_STDERR_HANDLER_ATTR = "_fila_api_io_stderr_handler"


def _ensure_api_io_stderr_handler() -> None:
    for handler in _logger.handlers:
        if getattr(handler, _STDERR_HANDLER_ATTR, False):
            return
    stream_handler = logging.StreamHandler(sys.stderr)
    setattr(stream_handler, _STDERR_HANDLER_ATTR, True)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(ReadableFormatter())
    _logger.addHandler(stream_handler)
    _logger.propagate = False


_ensure_api_io_stderr_handler()

_RECOMMEND_STAGE_LABELS: dict[str, str] = {
    "intent_extract": "意图解析",
    "image_understanding": "图像理解",
    "embedding_query": "Embedding（query）",
    "milvus_recall": "Milvus 召回",
    "es_recall": "ES 召回",
    "relations_pack": "sku_to_outfits / compatibility",
    "rank_truncate": "排序与截断",
    "reason_llm": "推荐理由 LLM",
    "response_build": "响应组装",
    "chat_begin": "会话开始",
    "chat_intent_parsed": "意图识别",
    "chat_image_and_embedding": "图像理解与向量",
    "chat_milvus_vector_recall": "向量召回(Milvus-图文)",
    "chat_milvus_text_vector_recall": "向量召回(Milvus-文本)",
    "text_vector_recall": "SKU文本向量召回",
    "query_keywords_extracted": "关键词抽取",
    "chat_anchor_final": "锚定SKU",
    "multi_path_recall": "三路并行召回",
    "chat_multi_recall_merged": "搭配召回-多路合并",
    "chat_branch_recall": "搭配召回-分支汇总",
    "chat_anchor_graph_recall": "搭配召回-相似固定搭配",
    "chat_text_vector_compose_recall": "搭配召回-文本向量拼套",
    "chat_query2es_compose_recall": "搭配召回-Query2ES 拼套",
    "chat_outfit_built": "搭配结果汇总",
    "chat_reason_generated": "搭配推荐理由",
    "chat_stream_complete": "对话流结束",
    "recommend_skus_skip": "SKU推荐-无锚点跳过",
    "recommend_skus_relations": "SKU关联召回",
    "recommend_skus_done": "SKU排序完成",
    "recommend_outfits_candidates": "套装文本召回候选",
    "recommend_outfits_done": "套装排序完成",
    "outfit_rank_scores": "搭配排序得分",
    "outfit_dedupe": "搭配去重",
    "text_vector_compose_skipped": "文本向量拼套跳过",
}


def _env_flag(names: tuple[str, ...]) -> bool | None:
    for name in names:
        raw = os.environ.get(name, "").strip().lower()
        if raw in ("1", "true", "yes", "on"):
            return True
        if raw in ("0", "false", "no", "off"):
            return False
    return None


def debug_api_io_enabled() -> bool:
    env = _env_flag(
        ("FILA_AGENT_DEBUG_API_IO", "FILA_V2_DEBUG_API_IO"),
    )
    if env is not None:
        return env
    cfg = load_config()
    log_cfg = cfg.get("logging") or {}
    return bool(log_cfg.get("debug_api_io"))


def debug_recommend_pipeline_enabled() -> bool:
    env = _env_flag(
        ("FILA_AGENT_DEBUG_PIPELINE", "FILA_V2_DEBUG_PIPELINE"),
    )
    if env is not None:
        return env
    cfg = load_config().get("logging") or {}
    if "debug_recommend_pipeline" in cfg:
        return bool(cfg.get("debug_recommend_pipeline"))
    return debug_api_io_enabled()


def record_recommend_timing_enabled() -> bool:
    cfg = load_config().get("logging") or {}
    return bool(cfg.get("record_recommend_timing", True))


def _redact_rules() -> dict[str, Any]:
    cfg = load_config()
    return (cfg.get("logging") or {}).get("redact") or {}


def redact_for_log(obj: Any, depth: int = 0) -> Any:
    if depth > 12:
        return "<max_depth>"
    rules = _redact_rules()
    max_chars = int(rules.get("prompt_max_chars") or 2000)
    redact_img = bool(rules.get("image_base64", True))
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k == "image_base64" and redact_img and isinstance(v, str) and v:
                out[k] = f"<redacted len={len(v)}>"
            else:
                out[k] = redact_for_log(v, depth + 1)
        return out
    if isinstance(obj, list):
        if len(obj) > 50:
            head = [redact_for_log(x, depth + 1) for x in obj[:50]]
            return head + [f"<... {len(obj) - 50} more>"]
        return [redact_for_log(x, depth + 1) for x in obj]
    if isinstance(obj, str) and len(obj) > max_chars:
        return obj[:max_chars] + f"<... len={len(obj)}>"
    return obj


def summarize_http_response(path: str, data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"path": path, "kind": type(data).__name__}
    if path.rstrip("/").endswith("/skus") or "groups" in data:
        groups = data.get("groups") or []
        return {
            "path": path,
            "anchor_sku_id": data.get("anchor_sku_id"),
            "group_count": len(groups),
            "roles": [
                {
                    "role": g.get("role"),
                    "sku_count": len(g.get("skus") or []),
                    "sku_ids": [
                        s.get("sku_id")
                        for s in (g.get("skus") or [])[:8]
                    ],
                }
                for g in groups
            ],
            "source_outfit_ids_count": len(
                data.get("source_outfit_ids") or [],
            ),
        }
    if "outfits" in data:
        outfits = data.get("outfits") or []
        return {
            "path": path,
            "outfit_count": len(outfits),
            "outfit_ids": [o.get("outfit_id") for o in outfits[:12]],
            "names": [o.get("name") for o in outfits[:6]],
        }
    return {"path": path, "keys": list(data.keys())}


def log_flow(tag: str, payload: dict[str, Any]) -> None:
    if not debug_api_io_enabled():
        return
    line = json.dumps({"tag": tag, **payload}, ensure_ascii=False, default=str)
    _logger.info("%s", line)


def _debug_recall_io_enabled() -> bool:
    return debug_api_io_enabled() or debug_recommend_pipeline_enabled()


def log_keywords_extracted(
    keywords: list[str],
    *,
    trace_id: str | None = None,
    sources: dict[str, Any] | None = None,
) -> None:
    """打印图文意图抽取到的检索关键词。"""
    if not _debug_recall_io_enabled():
        return
    payload: dict[str, Any] = {
        "trace_id": trace_id,
        "keywords": keywords,
        "keyword_count": len(keywords),
    }
    if sources is not None:
        payload["sources"] = redact_for_log(sources)
    log_flow("query_keywords_extracted", payload)
    if debug_recommend_pipeline_enabled():
        _logger.info(
            "[FILA穿搭管线] %s",
            json.dumps(
                {
                    "tag": "recommend_stage",
                    "stage": "query_keywords_extracted",
                    "环节": "关键词抽取",
                    "trace_id": trace_id,
                    "keywords": keywords,
                    "keyword_count": len(keywords),
                },
                ensure_ascii=False,
                default=str,
            ),
        )


def log_text_vector_recall_io(
    *,
    trace_id: str | None = None,
    collection: str,
    vector_field: str,
    top_k_per_keyword: int,
    min_similarity: float,
    per_keyword: list[dict[str, Any]],
    merged_output: list[dict[str, Any]],
) -> None:
    """文本向量 Milvus 检索：每个关键词的输入与命中，以及融合后的输出。"""
    if not _debug_recall_io_enabled():
        return
    log_flow(
        "text_vector_recall_io",
        {
            "trace_id": trace_id,
            "collection": collection,
            "vector_field": vector_field,
            "top_k_per_keyword": top_k_per_keyword,
            "min_similarity": min_similarity,
            "per_keyword": redact_for_log(per_keyword),
            "merged_output": merged_output,
            "merged_count": len(merged_output),
        },
    )


def log_outfit_rank_scores(
    ranked: list[tuple[float, Any]],
    *,
    trace_id: str | None = None,
    top_k: int | None = None,
) -> None:
    """排序后每套搭配的得分（含是否进入最终 Top-K）。"""
    if not _debug_recall_io_enabled() or not ranked:
        return
    lim = top_k if top_k is not None and top_k > 0 else len(ranked)
    rows: list[dict[str, Any]] = []
    for idx, (score, outfit) in enumerate(ranked, start=1):
        if not isinstance(outfit, dict):
            continue
        oid = str(outfit.get("outfit_id") or "")
        src = str(
            outfit.get("source")
            or outfit.get("_recall_path")
            or "",
        )
        sku_ids = [
            str(it.get("sku_id") or "")
            for it in (outfit.get("items") or [])
            if it.get("sku_id")
        ]
        rows.append(
            {
                "rank": idx,
                "outfit_id": oid,
                "score": round(float(score), 4),
                "name": str(outfit.get("name") or "")[:120],
                "recall_source": src,
                "in_top_k": idx <= lim,
                "sku_ids": sku_ids,
            },
        )
    payload: dict[str, Any] = {
        "trace_id": trace_id,
        "ranked_count": len(rows),
        "top_k": lim,
        "outfits": rows,
    }
    log_flow("outfit_rank_scores", payload)
    if debug_recommend_pipeline_enabled():
        _logger.info(
            "[FILA穿搭管线] %s",
            json.dumps(
                {
                    "tag": "recommend_stage",
                    "stage": "outfit_rank_scores",
                    "环节": _RECOMMEND_STAGE_LABELS["outfit_rank_scores"],
                    **payload,
                },
                ensure_ascii=False,
                default=str,
            ),
        )


def log_text_search_recall_io(
    *,
    trace_id: str | None = None,
    entity: str,
    channel: str,
    query: str,
    limit: int,
    output_ids: list[str],
    extra: dict[str, Any] | None = None,
) -> None:
    """全文/本地文本检索（非向量）的查询与结果 ID 列表。"""
    if not _debug_recall_io_enabled():
        return
    payload: dict[str, Any] = {
        "trace_id": trace_id,
        "entity": entity,
        "channel": channel,
        "input": {"query": (query or "")[:500], "limit": limit},
        "output": {
            "ids": output_ids[:50],
            "count": len(output_ids),
        },
    }
    if extra:
        payload.update(redact_for_log(extra))
    log_flow("text_search_recall_io", payload)


def log_recommend_stage(
    trace_id: str,
    stage: str,
    *,
    elapsed_ms: int | None = None,
    since_request_ms: int | None = None,
    **fields: Any,
) -> None:
    """推荐管线阶段日志；耗时与 debug 解耦（见技术方案 §11.4/11.5）。"""
    pipeline = debug_recommend_pipeline_enabled()
    timing = record_recommend_timing_enabled()
    if not pipeline and not timing:
        return
    label = _RECOMMEND_STAGE_LABELS.get(stage, stage)
    payload: dict[str, Any] = {
        "tag": "recommend_stage",
        "trace_id": trace_id,
        "stage": stage,
        "环节": label,
    }
    if elapsed_ms is not None:
        payload["elapsed_ms"] = int(elapsed_ms)
    if since_request_ms is not None:
        payload["since_request_ms"] = int(since_request_ms)
    if pipeline:
        payload.update(redact_for_log(fields))
    else:
        for k in (
            "anchor_sku_id",
            "outfit_count",
            "reason",
            "milvus_hit_count",
            "raw_relation_count",
            "recall_pathway",
            "召回通路",
            "anchor_pathway",
            "outfit_pathway",
            "sku_pathway",
            "锚点通路",
            "套装通路",
            "单品通路",
            "sku_group_count",
            "relation_outfit_count",
            "outfit_graph_count",
            "synthetic_outfits",
            "outfit_merged_count",
        ):
            if k in fields:
                payload[k] = redact_for_log(fields[k])
    line = json.dumps(payload, ensure_ascii=False, default=str)
    _logger.info("[FILA穿搭管线] %s", line)


def summarize_messages_for_llm(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, str):
            s = content
            if len(s) > 400:
                s = s[:400] + f"<... len={len(content)}>"
            out.append({"role": role, "content": s})
        elif isinstance(content, list):
            parts: list[dict[str, Any]] = []
            for p in content:
                if not isinstance(p, dict):
                    parts.append({"type": "unknown"})
                    continue
                pt = p.get("type")
                if pt == "text":
                    t = str(p.get("text") or "")
                    if len(t) > 200:
                        t = t[:200] + f"<... len={len(p.get('text') or '')}>"
                    parts.append({"type": "text", "text": t})
                elif pt == "image_url":
                    url = str((p.get("image_url") or {}).get("url") or "")
                    if url.startswith("data:"):
                        parts.append(
                            {"type": "image_url", "url": "<data_uri redacted>"},
                        )
                    else:
                        parts.append({"type": "image_url", "url": url[:120]})
                else:
                    parts.append({"type": pt})
            out.append({"role": role, "content": parts})
        else:
            out.append({"role": role, "content": type(content).__name__})
    return out
