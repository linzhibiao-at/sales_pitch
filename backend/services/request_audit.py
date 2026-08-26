"""对外请求审计落库到 ES（fila-requests 索引）。

纯函数 build_*_doc 便于单测；RequestAuditLogger 负责写/查，失败静默降级。
"""

from __future__ import annotations

import base64
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from backend.config import get_request_audit_enabled

logger = logging.getLogger(__name__)


def now_iso() -> str:
    """UTC + 本地时区 iso 字符串（与 jsonl_logger 口径一致）。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def build_input_block(
    *,
    input_sku_id: str = "",
    image_url: Optional[str] = None,
    image_base64: Optional[str] = None,
    message: Optional[str] = None,
    tryon: bool = False,
    reason_style: Optional[str] = None,
    outfit_id: Optional[str] = None,
) -> dict[str, Any]:
    """构造审计文档的 input 子结构；图片只存 url + 抓取字节 sha1。"""
    image_sha1: Optional[str] = None
    if image_base64:
        try:
            image_sha1 = hashlib.sha1(base64.b64decode(image_base64)).hexdigest()
        except Exception:  # noqa: BLE001
            image_sha1 = None
    return {
        "input_sku_id": input_sku_id or "",
        "image_url": image_url or None,
        "image_sha1": image_sha1,
        "message": message or None,
        "tryon": bool(tryon),
        "reason_style": reason_style or None,
        "outfit_id": outfit_id or None,
    }


def _slim_outfits(outfits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for card in outfits or []:
        items = []
        for it in card.get("items") or []:
            items.append({
                "sku_id": it.get("sku_id"),
                "role": it.get("role"),
                "title": it.get("title"),
                "spu_id": it.get("spu_id"),
                "id_goods": it.get("id_goods"),
            })
        out.append({
            "outfit_id": card.get("outfit_id"),
            "outfit_rank": card.get("outfit_rank"),
            "reason": card.get("reason"),
            "items": items,
        })
    return out


def build_recommend_doc(
    *,
    input_block: dict[str, Any],
    captured: dict[str, Any],
    meta: dict[str, Any],
) -> dict[str, Any]:
    """拼 recommend 审计文档。captured 为 chat_stream 采集的事件集合。"""
    intent_ev = captured.get("intent") or {}
    intent_block = None
    if intent_ev:
        intent_block = {
            "intent": intent_ev.get("intent"),
            "method": intent_ev.get("method"),
            "confidence": intent_ev.get("confidence"),
            "llm_fallback": intent_ev.get("llm_fallback"),
            "image_override": intent_ev.get("image_override"),
            "anchor_source": intent_ev.get("anchor_source"),
            "image_role": intent_ev.get("image_role"),
        }

    recall_ev = captured.get("recall_done") or {}
    anchor_skus = (captured.get("anchor_skus") or {}).get("skus") or []
    anchor_sku_id = anchor_skus[0].get("sku_id") if anchor_skus else None
    paths: dict[str, Any] = {}
    for pe in captured.get("recall_progress") or []:
        pname = pe.get("path")
        if pname:
            paths[pname] = {
                "count": pe.get("count", 0),
                "elapsed_ms": pe.get("elapsed_ms", 0),
            }
    recall_block = None
    if recall_ev:
        recall_block = {
            "anchor_sku_id": anchor_sku_id,
            "mode": recall_ev.get("mode"),
            "recalled_sku_count": recall_ev.get("recalled_sku_count", 0),
            "composed_outfit_count": recall_ev.get("composed_outfit_count", 0),
            "before_dedupe": recall_ev.get("before_dedupe", 0),
            "after_dedupe": recall_ev.get("after_dedupe", 0),
            "paths": paths,
            "roles": recall_ev.get("roles") or {},
        }

    ranking_ev = captured.get("ranking_reason_done") or {}
    ranking_block = None
    if ranking_ev:
        ranking_block = {
            "input_count": ranking_ev.get("input_count", 0),
            "output_count": ranking_ev.get("output_count", 0),
            "scoring_method": ranking_ev.get("scoring_method"),
            "ranking_elapsed_ms": ranking_ev.get("ranking_elapsed_ms", 0),
        }

    return {
        "trace_id": meta.get("trace_id"),
        "session_id": meta.get("session_id"),
        "app_id": meta.get("app_id"),
        "caller": meta.get("caller"),
        "request_kind": "recommend",
        "ts": meta.get("ts"),
        "elapsed_ms": meta.get("elapsed_ms"),
        "status": meta.get("status", "ok"),
        "error": meta.get("error"),
        "input": input_block,
        "intent": intent_block,
        "recall": recall_block,
        "ranking": ranking_block,
        "result": {"outfits": _slim_outfits(captured.get("outfits") or [])},
    }


def build_regenerate_doc(
    *,
    input_block: dict[str, Any],
    result: Optional[dict[str, Any]],
    meta: dict[str, Any],
) -> dict[str, Any]:
    """拼 regenerate_reason 审计文档；intent/recall/ranking 不适用，置 None。"""
    res_block: Optional[dict[str, Any]] = None
    if isinstance(result, dict):
        if "error" in result:
            res_block = {
                "outfit_id": result.get("outfit_id"),
                "reason": None,
                "error": result.get("error"),
            }
        else:
            res_block = {
                "outfit_id": result.get("outfit_id"),
                "reason": result.get("reason"),
            }
    return {
        "trace_id": meta.get("trace_id"),
        "session_id": meta.get("session_id"),
        "app_id": meta.get("app_id"),
        "caller": meta.get("caller"),
        "request_kind": "regenerate_reason",
        "ts": meta.get("ts"),
        "elapsed_ms": meta.get("elapsed_ms"),
        "status": meta.get("status", "ok"),
        "error": meta.get("error"),
        "input": input_block,
        "intent": None,
        "recall": None,
        "ranking": None,
        "result": res_block,
    }


def build_audit_search_body(filters: dict[str, Any]) -> dict[str, Any]:
    """构造审计列表查询 body：字符串字段走 .keyword 精确匹配 + ts 倒序 + 分页。"""
    must: list[dict[str, Any]] = []

    def add_term(field: str, val: Any) -> None:
        if val:
            must.append({"term": {f"{field}.keyword": str(val)}})

    add_term("trace_id", filters.get("trace_id"))
    add_term("app_id", filters.get("app_id"))
    add_term("session_id", filters.get("session_id"))
    add_term("request_kind", filters.get("request_kind"))
    add_term("status", filters.get("status"))

    rng: dict[str, Any] = {}
    if filters.get("ts_from"):
        rng["gte"] = str(filters["ts_from"])
    if filters.get("ts_to"):
        rng["lte"] = str(filters["ts_to"])
    if rng:
        must.append({"range": {"ts": rng}})

    query: dict[str, Any]
    if must:
        query = {"bool": {"must": must}}
    else:
        query = {"match_all": {}}

    size = max(1, min(int(filters.get("size") or 50), 200))
    offset = max(0, int(filters.get("offset") or 0))
    return {
        "size": size,
        "from": offset,
        "query": query,
        "sort": [{"ts": {"order": "desc"}}],
    }


def slim_audit_row(src: dict[str, Any]) -> dict[str, Any]:
    """审计列表精简行（详情另调 /api/audit/requests/{trace_id}）。"""
    result = src.get("result") or {}
    return {
        "trace_id": src.get("trace_id"),
        "session_id": src.get("session_id"),
        "app_id": src.get("app_id"),
        "request_kind": src.get("request_kind"),
        "ts": src.get("ts"),
        "elapsed_ms": src.get("elapsed_ms"),
        "status": src.get("status"),
        "input_sku_id": (src.get("input") or {}).get("input_sku_id"),
        "outfit_id": (src.get("input") or {}).get("outfit_id"),
        "outfit_count": len(result.get("outfits") or []),
    }


class RequestAuditLogger:
    """对外请求审计 ES 写/查；不可用或关闭时静默降级。"""

    def __init__(
        self,
        es: Any = None,
        enabled: Optional[bool] = None,
    ) -> None:
        if es is not None:
            self._es = es
        else:
            from backend.retrieval.es_client import EsClient
            self._es = EsClient()
        self._enabled = (
            get_request_audit_enabled() if enabled is None else bool(enabled)
        )

    @property
    def enabled(self) -> bool:
        return bool(self._enabled) and bool(
            getattr(self._es, "available", False),
        )

    def write(self, doc: dict[str, Any]) -> None:
        """写一条审计文档；关闭/不可用/失败均静默。"""
        if not self._enabled:
            return
        try:
            self._es.index_doc("requests", doc, refresh=False)
        except Exception:  # noqa: BLE001
            logger.warning("request audit write failed", exc_info=True)

    def search(self, body: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        if not self.enabled:
            return []
        try:
            return self._es.search_docs("requests", body)
        except Exception:  # noqa: BLE001
            logger.warning("request audit search failed", exc_info=True)
            return []

    def get_by_trace_id(self, trace_id: str) -> Optional[dict[str, Any]]:
        if not self.enabled or not trace_id:
            return None
        rows = self.search({
            "size": 1,
            "query": {"term": {"trace_id.keyword": str(trace_id)}},
        })
        return rows[0][1] if rows else None
