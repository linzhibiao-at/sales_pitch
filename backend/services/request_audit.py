"""对外请求审计：写经内存队列后台批量落库 MySQL（request_audit 表）。

纯函数 build_*_doc 便于单测；RequestAuditLogger 负责写/查，失败静默降级。
写路径不入业务线程：``write()`` 仅入队，由 ``audit_worker`` 后台线程
批量写，MySQL 慢/断连不阻塞话术主链路。
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


def build_audit_query(filters: dict[str, Any]) -> tuple[str, list[Any], int, int]:
    """构造审计列表 SQL 条件：返回 ``(WHERE, params, limit, offset)``。"""
    clauses: list[str] = []
    params: list[Any] = []

    for col, key in [
        ("trace_id", "trace_id"),
        ("app_id", "app_id"),
        ("session_id", "session_id"),
        ("request_kind", "request_kind"),
        ("status", "status"),
    ]:
        val = filters.get(key)
        if val:
            clauses.append(f"{col} = %s")
            params.append(str(val))

    ts_from = filters.get("ts_from")
    if ts_from:
        clauses.append("ts >= %s")
        params.append(str(ts_from))
    ts_to = filters.get("ts_to")
    if ts_to:
        clauses.append("ts <= %s")
        params.append(str(ts_to))

    where = "WHERE " + " AND ".join(clauses) if clauses else ""

    limit = max(1, min(int(filters.get("size") or 50), 200))
    offset = max(0, int(filters.get("offset") or 0))

    return where, params, limit, offset


def slim_audit_row(src: dict[str, Any]) -> dict[str, Any]:
    """审计列表精简行（详情另调 /v1/audit/requests/{trace_id}）。"""
    result = src.get("result") or {}
    input_block = src.get("input") or {}
    pitch = str(result.get("pitch") or "")
    return {
        "trace_id": src.get("trace_id"),
        "session_id": src.get("session_id"),
        "app_id": src.get("app_id"),
        "request_kind": src.get("request_kind"),
        "ts": src.get("ts"),
        "created_at": src.get("created_at") or src.get("ts"),
        "elapsed_ms": src.get("elapsed_ms"),
        "status": src.get("status"),
        "pitch_style": input_block.get("pitch_style"),
        "pitch": pitch[:80],
        "product_count": len(input_block.get("products") or []),
        "has_customer": bool(input_block.get("customer")),
        "pitch_len": result.get("pitch_len") or 0,
    }


class RequestAuditLogger:
    """对外请求审计 MySQL 写/查；写经内存队列后台批量，失败静默降级。

    ``start_worker=False`` 用于纯查询实例（如审计路由），不起写线程。
    """

    def __init__(
        self,
        client: Any = None,
        enabled: Optional[bool] = None,
        worker: Any = None,
        start_worker: bool = True,
    ) -> None:
        self._enabled = (
            get_request_audit_enabled() if enabled is None else bool(enabled)
        )
        if client is not None:
            self._client = client
        elif self._enabled:
            from backend.infra.mysql import MysqlClient
            self._client = MysqlClient()
        else:
            # 审计关闭时不建连接，避免无谓的 MySQL 握手
            self._client = None
        if worker is not None:
            self._worker = worker
        elif (
            self._enabled and start_worker
            and self._client is not None
            and getattr(self._client, "available", True)
        ):
            # available=False（url 为空/连接失败）时不起空转线程；
            # 入队前提 enabled 同样依赖 available，两者口径一致
            from backend.services.audit_worker import AuditBatchWorker
            self._worker = AuditBatchWorker(self._client)
        else:
            self._worker = None

    @property
    def enabled(self) -> bool:
        return bool(self._enabled) and bool(
            getattr(self._client, "available", False),
        )

    def write(self, doc: dict[str, Any]) -> None:
        """审计文档入队（后台线程批量写 MySQL）；关闭/队列满均静默。"""
        if not self._enabled or self._worker is None:
            return
        try:
            self._worker.submit(doc)
        except Exception:  # noqa: BLE001
            logger.warning("request audit submit failed", exc_info=True)

    def flush(self, timeout: float = 10.0) -> bool:
        """等待队列中审计全部落库（测试/运维用）。"""
        if self._worker is None:
            return True
        return self._worker.flush(timeout)

    def close(self, timeout: float = 10.0) -> None:
        """停止后台写线程并尽力 drain 剩余队列（进程退出由 atexit 兜底）。"""
        if self._worker is not None:
            self._worker.close(timeout)

    def search(
        self,
        where: str,
        params: list | tuple,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """按条件查询审计列表（ts 倒序）。"""
        if not self.enabled:
            return []
        try:
            return self._client.query_audit(where, params, limit, offset)
        except Exception:  # noqa: BLE001
            logger.warning("request audit search failed", exc_info=True)
            return []

    def count(self, where: str, params: list | tuple) -> int:
        """按条件统计总数。"""
        if not self.enabled:
            return 0
        try:
            return self._client.count_audit(where, params)
        except Exception:  # noqa: BLE001
            logger.warning("request audit count failed", exc_info=True)
            return 0

    def get_by_trace_id(self, trace_id: str) -> Optional[dict[str, Any]]:
        if not self.enabled or not trace_id:
            return None
        return self._client.get_by_trace_id(trace_id)
