"""审计路由（只读）：GET /api/audit/requests[/{trace_id}]。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException

from backend.routers.sales_pitch import get_pitch_service
from backend.services.request_audit import build_audit_search_body, slim_audit_row

router = APIRouter()


@router.get("/api/audit/requests")
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
) -> dict:
    """请求审计列表（只读，按 ts 倒序）。审计关闭/ES 不可用时返空。"""
    audit = get_pitch_service()._audit  # noqa: SLF001
    if not audit.enabled:
        return {"enabled": False, "items": []}
    body = build_audit_search_body({
        "trace_id": trace_id, "app_id": app_id, "session_id": session_id,
        "request_kind": request_kind, "status": status,
        "ts_from": ts_from, "ts_to": ts_to, "size": size, "offset": offset,
    })
    rows = audit.search(body)
    return {"enabled": True, "items": [slim_audit_row(s) for _, s in rows]}


@router.get("/api/audit/requests/{trace_id}")
def api_audit_request_detail(trace_id: str) -> dict:
    """请求审计详情（完整文档）。审计不可用 503，未命中 404。"""
    audit = get_pitch_service()._audit  # noqa: SLF001
    if not audit.enabled:
        raise HTTPException(status_code=503, detail="audit disabled")
    doc = audit.get_by_trace_id((trace_id or "").strip())
    if not doc:
        raise HTTPException(status_code=404, detail="not found")
    return doc
