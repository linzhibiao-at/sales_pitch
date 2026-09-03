"""审计路由（只读）：GET /v1/audit/requests[/{trace_id}]。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException

from backend.services.request_audit import (
    RequestAuditLogger,
    build_audit_query,
    slim_audit_row,
)

router = APIRouter()

# 独立审计实例（不依赖 Agent 服务是否可用）
_audit = RequestAuditLogger(start_worker=False)  # 纯查询实例，不起后台写线程


@router.get("/requests")
def api_audit_requests(
    trace_id: Optional[str] = None,
    app_id: Optional[str] = None,
    session_id: Optional[str] = None,
    request_kind: Optional[str] = None,
    status: Optional[str] = None,
    ts_from: Optional[str] = None,
    ts_to: Optional[str] = None,
    size: int = 50,
    offset: int = 0,
    page: Optional[int] = None,
) -> dict:
    """请求审计列表（只读，按 ts 倒序）。审计关闭/MySQL 不可用时返空。

    ``page``（1 起）与 ``offset`` 二选一：page 优先，offset = (page-1)*size。
    """
    if not _audit.enabled:
        return {"enabled": False, "total": 0, "items": []}
    if page is not None and page >= 1:
        offset = (page - 1) * size
    filters = {
        "trace_id": trace_id, "app_id": app_id, "session_id": session_id,
        "request_kind": request_kind, "status": status,
        "ts_from": ts_from, "ts_to": ts_to, "size": size, "offset": offset,
    }
    where, params, limit, offset = build_audit_query(filters)
    rows = _audit.search(where, params, limit, offset)
    total = _audit.count(where, params)
    return {
        "enabled": True,
        "total": total,
        "items": [slim_audit_row(r) for r in rows],
    }


@router.get("/requests/{trace_id}")
def api_audit_request_detail(trace_id: str) -> dict:
    """请求审计详情（完整文档）。审计不可用 503，未命中 404。"""
    if not _audit.enabled:
        raise HTTPException(status_code=503, detail="audit disabled")
    doc = _audit.get_by_trace_id((trace_id or "").strip())
    if not doc:
        raise HTTPException(status_code=404, detail="not found")
    return doc
