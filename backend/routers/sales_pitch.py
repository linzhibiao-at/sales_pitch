"""营销话术路由：POST /v1/sales-pitch/generate。"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.api_debug import log_flow, summarize_http_response
from backend.auth import verify_api_key
from backend.config import get_allowed_app_ids
from backend.models import SalesPitchRequest
from backend.services.sales_pitch_service import SalesPitchService

logger = logging.getLogger(__name__)

router = APIRouter()

# 进程级服务单例（与 worker 生命周期一致）
_pitch_svc = SalesPitchService()


def get_pitch_service() -> SalesPitchService:
    """暴露给测试/其他路由获取服务实例（避免跨模块访问私有变量）。"""
    return _pitch_svc


def _request_trace_id(request: Request) -> str:
    """取请求级 trace_id（中间件注入 request.state，缺失时兜底现生成）。"""
    return getattr(request.state, "trace_id", None) or uuid.uuid4().hex


@router.post("/generate")
async def v1_sales_pitch_generate(
    body: SalesPitchRequest,
    request: Request,
    _auth: None = Depends(verify_api_key),
) -> dict:
    """对外营销话术生成接口：顾客信息 + 商品信息 → 导购话术。"""
    app_id = (body.app_id or "").strip()
    if not app_id:
        raise HTTPException(status_code=400, detail="app_id required")
    # app_id 白名单（配置驱动；键缺失=不强制）。非白名单 → 401。
    allowed = get_allowed_app_ids()
    if allowed is not None and app_id not in allowed:
        raise HTTPException(status_code=401, detail="invalid app_id")
    # API Key 绑定校验: body.app_id 须与 Key 绑定的 app_id 一致(auth.enabled 时)
    caller = getattr(request.state, "caller", None)
    if caller is not None and app_id != caller.get("app_id"):
        raise HTTPException(
            status_code=401, detail="app_id mismatch with API key"
        )
    caller_app_id = (
        caller.get("app_id") if isinstance(caller, dict) else None
    )
    out = await _pitch_svc.generate(
        body,
        trace_id=_request_trace_id(request),
        app_id=app_id,
        caller=caller_app_id,
    )
    # LLM 空输出/上游故障 → 503（依赖服务不可用），带 trace_id 供联查
    if "error" in out:
        raise HTTPException(status_code=503, detail=out["error"])
    out["trace_id"] = _request_trace_id(request)
    log_flow(
        "http_out",
        summarize_http_response("/v1/sales-pitch/generate", out),
    )
    return out
