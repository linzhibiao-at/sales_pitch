"""对外请求审计落库到 ES（fila-requests 索引）。

纯函数 build_*_doc 便于单测；RequestAuditLogger 负责写/查，失败静默降级。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from backend.config import get_request_audit_enabled

logger = logging.getLogger(__name__)


def now_iso() -> str:
    """UTC + 本地时区 iso 字符串。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def build_sales_pitch_doc(
    *,
    input_block: dict[str, Any],
    result: Optional[dict[str, Any]],
    meta: dict[str, Any],
) -> dict[str, Any]:
    """拼 sales_pitch 审计文档；intent/recall/ranking 不适用，置 None。

    result 为 ``{"pitch": str}`` 或 ``{"error": str}``；话术正文可能较长，
    审计仅落前 600 字 + 长度，避免文档膨胀。
    """
    res_block: Optional[dict[str, Any]] = None
    if isinstance(result, dict):
        if "error" in result:
            res_block = {"pitch": None, "pitch_len": 0, "error": result.get("error")}
        else:
            pitch = str(result.get("pitch") or "")
            res_block = {
                "pitch": pitch[:600],
                "pitch_len": len(pitch),
            }
    return {
        "trace_id": meta.get("trace_id"),
        "session_id": meta.get("session_id"),
        "app_id": meta.get("app_id"),
        "caller": meta.get("caller"),
        "request_kind": "sales_pitch",
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
    input_block = src.get("input") or {}
    return {
        "trace_id": src.get("trace_id"),
        "session_id": src.get("session_id"),
        "app_id": src.get("app_id"),
        "request_kind": src.get("request_kind"),
        "ts": src.get("ts"),
        "elapsed_ms": src.get("elapsed_ms"),
        "status": src.get("status"),
        "product_count": len(input_block.get("products") or []),
        "has_customer": bool(input_block.get("customer")),
        "pitch_len": result.get("pitch_len") or 0,
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
            from backend.es_client import EsClient
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
