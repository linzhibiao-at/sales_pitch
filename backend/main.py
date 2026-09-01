"""FastAPI 应用入口：FILA 营销话术生成服务。

架构分层
    main.py                    ← app 骨架 / 中间件 / 异常处理器 / 路由挂载
    routers/sales_pitch.py     ← POST /v1/sales-pitch/generate
    routers/audit.py           ← GET /v1/audit/requests[/{trace_id}]
    services/sales_pitch_service.py  ← 业务编排（prompt → LLM → 审计）
    services/request_audit.py  ← 审计文档构造 + MySQL 写/查
"""

from __future__ import annotations

import json
import logging
import uuid

# ── 日志初始化：必须在其他 backend.* 导入之前，确保所有 logger 共享同一 formatter ──
from backend.logging_config import setup_logging as _setup_logging

_setup_logging()

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from backend.api_debug import (
    debug_api_io_enabled,
    log_flow,
    redact_for_log,
)
from backend.routers import audit, sales_pitch,user

logger = logging.getLogger(__name__)

app = FastAPI(title="FILA Sales Pitch Service", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 工具函数 ──────────────────────────────────────────────────────────────

def _error_envelope(status: int, message: str, trace_id: str) -> JSONResponse:
    """统一对外错误出参：``{"code": <status>, "message": <str>, "trace_id": <hex>}``。"""
    return JSONResponse(
        status_code=status,
        content={
            "code": status,
            "message": (message or "")[:500],
            "trace_id": trace_id,
        },
    )


def _fmt_validation_errors(errors: list) -> str:
    """把 RequestValidationError 的 errors() 压成一行可读串。"""
    parts = []
    for e in errors or []:
        loc = ".".join(str(x) for x in e.get("loc", []) if x != "body")
        parts.append(f"{loc or 'body'}: {e.get('msg', '')}".strip(": "))
    return "; ".join(parts) or "validation error"


def _request_trace_id(request: Request) -> str:
    """取请求级 trace_id；中间件已注入 request.state，缺失时兜底现生成。"""
    return getattr(request.state, "trace_id", None) or uuid.uuid4().hex


# ── 异常处理器 ────────────────────────────────────────────────────────────

@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    """400/404/.../5xx（含业务 raise 的 HTTPException）统一成 envelope。"""
    trace_id = _request_trace_id(request)
    status = exc.status_code or 500
    message = str(exc.detail) if exc.detail is not None else ""
    level = logging.ERROR if status >= 500 else logging.DEBUG
    logger.log(
        level,
        "[http_exc] %s %s -> %s trace_id=%s: %s",
        request.method, request.url.path, status, trace_id, message,
    )
    return _error_envelope(status, message, trace_id)


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    """422 入参校验失败统一成 envelope。"""
    trace_id = _request_trace_id(request)
    message = _fmt_validation_errors(exc.errors())
    logger.debug(
        "[validation] %s %s trace_id=%s: %s",
        request.method, request.url.path, trace_id, message,
    )
    return _error_envelope(422, message, trace_id)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    """全局兜底：未捕获异常 → 500 + trace_id，真实堆栈只进日志不外泄。"""
    trace_id = _request_trace_id(request)
    logger.error(
        "[unhandled] %s %s trace_id=%s: %s",
        request.method, request.url.path, trace_id, exc,
        exc_info=True,
    )
    return _error_envelope(500, f"{type(exc).__name__}: {exc}", trace_id)


# ── 中间件 ────────────────────────────────────────────────────────────────

@app.middleware("http")
async def trace_id_middleware(request: Request, call_next):
    """每请求生成 trace_id 注入 request.state，并回写 ``X-Trace-Id`` 响应头。

    对外接口成功出参与错误 envelope 共用同一 trace_id，调用方可凭此联调
    服务端日志；中间件最先执行，异常处理器经 ``request.state`` 复用之，
    保证成功/失败路径 trace_id 一致。
    """
    tid = uuid.uuid4().hex
    request.state.trace_id = tid
    response = await call_next(request)
    response.headers["X-Trace-Id"] = tid
    # API Key 鉴权的并发排队信息(backend/auth.py 注入 request.state.queue_info)
    qi = getattr(request.state, "queue_info", None)
    if qi:
        response.headers["X-Queue-Status"] = str(qi.get("queue_status", ""))
        response.headers["X-Queue-Wait"] = str(qi.get("queue_wait", ""))
        response.headers["X-Queue-Position"] = str(qi.get("queue_position", ""))
    return response


@app.middleware("http")
async def debug_api_request_middleware(request: Request, call_next):
    if not debug_api_io_enabled():
        return await call_next(request)
    method = request.method
    path = request.url.path
    body_bytes = b""
    if method == "POST" and path == "/v1/sales-pitch/generate":
        body_bytes = await request.body()

        async def receive():
            return {"type": "http.request", "body": body_bytes, "more_body": False}

        request = Request(request.scope, receive)
    if body_bytes:
        try:
            body_preview = redact_for_log(
                json.loads(body_bytes.decode("utf-8")),
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            body_preview = {"_parse_error": True, "raw_len": len(body_bytes)}
        log_flow(
            "http_in",
            {"path": path, "method": method, "body": body_preview},
        )
    response = await call_next(request)
    return response


# ── 健康检查 + 路由挂载 ─────────────────────────────────────────────────

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "sales_pitch"}



# 业务路由
app.include_router(sales_pitch.router, prefix="/v1/sales-pitch", tags=["sales_pitch"])
app.include_router(audit.router, prefix="/v1/audit", tags=["audit"])
app.include_router(user.router, prefix="/v1/users", tags=["user"])