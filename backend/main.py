"""FastAPI：FILA 推荐 API + HTML 调试台 + 商品图 Debug。"""

from __future__ import annotations

import json
import logging
import uuid
from typing import List, Optional

# ── 日志初始化：必须在其他 backend.* 导入之前，确保所有 logger 共享同一 formatter ──
from backend.logging_config import setup_logging as _setup_logging

_setup_logging()

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from backend.api_debug import (
    debug_api_io_enabled,
    log_flow,
    redact_for_log,
    summarize_http_response,
)
from backend.config import get_allowed_app_ids, get_root
from backend.auth import verify_api_key
from backend.models import (
    AGE_CANONICAL,
    ChatRequest,
    ExternalRecommendRequest,
    ExternalRegenerateReasonRequest,
    GENDER_CANONICAL,
    RecommendOutfitsRequest,
    RecommendSkusRequest,
    RegenerateReasonRequest,
    SEASON_CANONICAL,
    is_valid_sku_id_format,
)
from backend.prompt_loader import validate_prompt_paths
from backend.intent.sku_attributes import COLOR_SERIES_BASE_VALUES
from backend.recall_paths_config import get_ui_config
from backend.search_debug.ann_service import (
    get_ann_status,
    init_ann_search,
    search_neighbors,
    shutdown_ann,
)
from backend.search_debug.es_service import (
    get_es_config,
    search_es_direct,
    search_es_smart,
)
from backend.search_debug.milvus_service import (
    get_milvus_config,
    milvus_hybrid_debug,
)
from backend.services.recommend_service import RecommendService
from backend.services.request_audit import (
    build_audit_search_body,
    slim_audit_row,
)
from eval.review_store import get_review_store

errs = validate_prompt_paths()
if errs:
    import logging

    logging.getLogger("fila_agent_html").warning(
        "prompt 校验未通过（服务仍启动，LLM 可能失败）: %s",
        errs,
    )

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """初始化 ANN 检索调试服务。"""
    import logging

    _log = logging.getLogger("fila_agent_html")
    try:
        init_ann_search(app)
    except Exception as exc:
        _log.warning(
            "ANN 检索调试服务初始化失败（不影响主服务）: %s", exc,
        )
    yield
    try:
        shutdown_ann()
    except Exception:
        pass


app = FastAPI(title="FILA Outfits Agent (HTML)", version="0.1.0", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    """400/404/.../5xx（含业务 raise 的 HTTPException）统一成 envelope。"""
    trace_id = _request_trace_id(request)
    status = exc.status_code or 500
    message = str(exc.detail) if exc.detail is not None else ""
    level = logging.ERROR if status >= 500 else logging.DEBUG
    logging.getLogger("fila_agent_html").log(
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
    logging.getLogger("fila_agent_html").debug(
        "[validation] %s %s trace_id=%s: %s",
        request.method, request.url.path, trace_id, message,
    )
    return _error_envelope(422, message, trace_id)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    """全局兜底：未捕获异常 → 500 + trace_id，真实堆栈只进日志不外泄。"""
    trace_id = _request_trace_id(request)
    logging.getLogger("fila_agent_html").error(
        "[unhandled] %s %s trace_id=%s: %s",
        request.method, request.url.path, trace_id, exc,
        exc_info=True,
    )
    return _error_envelope(500, f"{type(exc).__name__}: {exc}", trace_id)



_svc = RecommendService()
_ROOT = get_root()
_web = _ROOT / "web"
_viewer = _ROOT / "image-debug-viewer"
_outfits_viewer = _ROOT / "outfits-viewer"
_eval = _ROOT / "eval"


class OutfitMgetBody(BaseModel):
    outfit_ids: list[str]


if _web.is_dir():
    app.mount("/web", StaticFiles(directory=str(_web)), name="web")

if _viewer.is_dir():
    app.mount(
        "/debug-static",
        StaticFiles(directory=str(_viewer), html=True),
        name="debug-static",
    )

if _outfits_viewer.is_dir():
    app.mount(
        "/outfits-viewer",
        StaticFiles(directory=str(_outfits_viewer), html=True),
        name="outfits-viewer",
    )


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
    if method == "POST" and path in (
        "/recommend/skus",
        "/recommend/outfits",
        "/v1/outfit/recommend",
        "/v1/outfit/regenerate-reason",
        "/chat",
    ):
        body_bytes = await request.body()

        async def receive():
            return {"type": "http.request", "body": body_bytes, "more_body": False}

        request = Request(request.scope, receive)
    body_preview = None
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
    if path == "/chat" and debug_api_io_enabled():
        log_flow(
            "http_out",
            {"path": path, "kind": "sse_stream"},
        )
    return response


@app.get("/")
def index_page():
    """穿搭推荐 HTML 调试台。"""
    index = _web / "index.html"
    if not index.is_file():
        return RedirectResponse(url="/web/index.html")
    html = index.read_text(encoding="utf-8")
    if get_ui_config().get("ui_mode") == "presentation":
        # 对外展示模式：首帧即给 body 加 presentation class，
        # 让 .hide-in-presentation（如批量评测入口）在 CSS 层立即隐藏，
        # 不依赖前端异步 fetch /api/ui-config，避免首屏闪现调试入口。
        html = html.replace("<body>", '<body class="presentation">', 1)
    return HTMLResponse(html)


@app.get("/products")
def products_page():
    """商品浏览页（clean URL，原 /outfits-viewer/browse.html）。

    资源仍挂在 /outfits-viewer 静态目录下，故 browse.html/browse.js 内的
    同目录引用走绝对 /outfits-viewer/ 前缀。
    """
    browse = _outfits_viewer / "browse.html"
    if browse.is_file():
        return FileResponse(browse)
    raise HTTPException(status_code=404, detail="browse.html not found")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "fila_agent_html"}


@app.get("/api/ui-config")
def api_ui_config() -> dict:
    """调试台 UI 开关（与 config.yaml recommend 对齐）。"""
    return get_ui_config()


@app.post("/recommend/skus")
def recommend_skus(body: RecommendSkusRequest) -> dict:
    out = _svc.recommend_skus(body)
    log_flow(
        "http_out",
        summarize_http_response("/recommend/skus", out),
    )
    return out


@app.post("/recommend/outfits")
def recommend_outfits(body: RecommendOutfitsRequest) -> dict:
    out = _svc.recommend_outfits(body)
    log_flow(
        "http_out",
        summarize_http_response("/recommend/outfits", out),
    )
    return out


@app.post("/chat")
async def chat(body: ChatRequest):
    async def gen():
        async for ev in _svc.chat_stream(body):
            chunk = json.dumps(ev, ensure_ascii=False)
            yield f"data: {chunk}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/regenerate-reason")
def regenerate_reason(body: RegenerateReasonRequest) -> dict:
    result = _svc.regenerate_outfit_reason(body)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.post("/v1/outfit/recommend")
async def v1_outfit_recommend(
    body: ExternalRecommendRequest,
    request: Request,
    _auth: None = Depends(verify_api_key),
) -> dict:
    """对外搭配推荐接口（按 docs/FILA穿搭推荐入参出参.md）。"""
    app_id = (body.app_id or "").strip()
    if not app_id:
        raise HTTPException(status_code=400, detail="app_id required")
    # ISS-04: app_id 白名单(配置驱动; 键缺失=不强制)。非白名单 → 401。
    allowed = get_allowed_app_ids()
    if allowed is not None and app_id not in allowed:
        raise HTTPException(status_code=401, detail="invalid app_id")
    # API Key 绑定校验: body.app_id 须与 Key 绑定的 app_id 一致(auth.enabled 时)
    caller = getattr(request.state, "caller", None)
    if caller is not None and app_id != caller.get("app_id"):
        raise HTTPException(
            status_code=401, detail="app_id mismatch with API key"
        )
    input_sku_id = (body.input_sku_id or "").strip()
    if not any([input_sku_id, (body.image_url or "").strip(), (body.message or "").strip()]):
        raise HTTPException(
            status_code=400,
            detail="at least one of input_sku_id/image_url/message required",
        )
    # ISS-02: 锚点 SKU 基本格式校验; 仅拒格式垃圾(小写/中文/符号/纯数字/SQL/XSS),
    # 格式合法但查不到仍走 200+空降级。
    if input_sku_id and not is_valid_sku_id_format(input_sku_id):
        raise HTTPException(status_code=400, detail="invalid sku_id format")
    caller_app_id = (
        caller.get("app_id") if isinstance(caller, dict) else None
    )
    out = await _svc.external_recommend(
        body,
        trace_id=_request_trace_id(request),
        app_id=app_id,
        caller=caller_app_id,
    )
    out["trace_id"] = _request_trace_id(request)
    log_flow(
        "http_out",
        summarize_http_response("/v1/outfit/recommend", out),
    )
    return out


@app.post("/v1/outfit/regenerate-reason")
def v1_outfit_regenerate(
    body: ExternalRegenerateReasonRequest,
    request: Request,
    _auth: None = Depends(verify_api_key),
) -> dict:
    """对外重新生成推荐理由接口（按 docs/FILA穿搭推荐入参出参.md）。"""
    caller_info = getattr(request.state, "caller", None)
    caller_app_id = (
        caller_info.get("app_id")
        if isinstance(caller_info, dict) else None
    )
    result = _svc.external_regenerate(
        body,
        trace_id=_request_trace_id(request),
        app_id=caller_app_id,
        caller=caller_app_id,
    )
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    result["trace_id"] = _request_trace_id(request)
    return result


@app.get("/api/audit/requests")
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
    """对外请求审计列表（只读，按 ts 倒序）。审计关闭/ES 不可用时返空。"""
    audit = _svc._audit  # noqa: SLF001
    if not audit.enabled:
        return {"enabled": False, "items": []}
    body = build_audit_search_body({
        "trace_id": trace_id, "app_id": app_id, "session_id": session_id,
        "request_kind": request_kind, "status": status,
        "ts_from": ts_from, "ts_to": ts_to, "size": size, "offset": offset,
    })
    rows = audit.search(body)
    return {"enabled": True, "items": [slim_audit_row(s) for _, s in rows]}


@app.get("/api/audit/requests/{trace_id}")
def api_audit_request_detail(trace_id: str) -> dict:
    """对外请求审计详情（完整文档）。审计不可用 503，未命中 404。"""
    audit = _svc._audit  # noqa: SLF001
    if not audit.enabled:
        raise HTTPException(status_code=503, detail="audit disabled")
    doc = audit.get_by_trace_id((trace_id or "").strip())
    if not doc:
        raise HTTPException(status_code=404, detail="not found")
    return doc


@app.get("/skus/{sku_id}")
def get_sku(sku_id: str) -> dict:
    row = _svc._data.get_sku(sku_id)  # noqa: SLF001
    if not row:
        raise HTTPException(status_code=404, detail="sku not found")
    return row


@app.get("/spus/{spu_id}/skus")
def get_spu_skus(spu_id: str) -> dict:
    ids = _svc._data.expand_spu(spu_id)  # noqa: SLF001
    rows = [
        row
        for row in (_svc._data.get_sku(i) for i in ids)  # noqa: SLF001
        if row
    ]
    return {"spu_id": spu_id, "skus": rows}


@app.get("/api/outfits/sources")
def outfit_sources() -> dict:
    rows = _svc._data.outfit_source_counts()  # noqa: SLF001
    total = sum(int(row.get("count") or 0) for row in rows)
    return {"total": total, "sources": rows}


@app.get("/api/outfits/color-series")
def outfit_color_series(source: Optional[str] = None) -> dict:
    source_key = (source or "").strip() or None
    rows = _svc._data.outfit_color_series_counts(  # noqa: SLF001
        source=source_key,
    )
    total = sum(int(row.get("count") or 0) for row in rows)
    return {
        "total": total,
        "source": source_key,
        "color_series": rows,
    }


@app.get("/api/outfits/season")
def outfit_season(source: Optional[str] = None) -> dict:
    source_key = (source or "").strip() or None
    rows = _svc._data.outfit_season_counts(  # noqa: SLF001
        source=source_key,
    )
    total = sum(int(row.get("count") or 0) for row in rows)
    return {
        "total": total,
        "source": source_key,
        "season": rows,
    }


@app.get("/api/outfits")
def browse_outfits(
    offset: int = 0,
    size: int = 80,
    source: Optional[str] = None,
    color_series: Optional[str] = None,
    season: Optional[str] = None,
) -> dict:
    source_key = (source or "").strip() or None
    color_key = (color_series or "").strip() or None
    season_key = (season or "").strip() or None
    rows, total = _svc._data.browse_outfits(  # noqa: SLF001
        offset=offset,
        size=size,
        source=source_key,
        color_series=color_key,
        season=season_key,
    )
    return {
        "offset": max(0, int(offset)),
        "size": max(1, int(size)),
        "source": source_key,
        "color_series": color_key,
        "season": season_key,
        "total": total,
        "outfits": rows,
    }


@app.get("/api/skus")
def browse_skus(
    offset: int = 0,
    size: int = 60,
    gender: Optional[List[str]] = Query(default=None),
    age: Optional[List[str]] = Query(default=None),
    season: Optional[List[str]] = Query(default=None),
    color_series: Optional[List[str]] = Query(default=None),
    category_l2: Optional[List[str]] = Query(default=None),
    series: Optional[List[str]] = Query(default=None),
    role: Optional[List[str]] = Query(default=None),
    up_time_since: Optional[str] = Query(
        default=None,
        description="上架时间下限（yyyy-MM-dd，UTC 口径），仅检索此后上架的 SKU",
    ),
) -> dict:
    """按结构化筛选分页浏览 SKU（镜像 /api/outfits）。各维度间 AND、维度内多值 OR。"""
    since_key = (up_time_since or "").strip() or None
    rows, total = _svc._data.browse_skus(  # noqa: SLF001
        offset=offset,
        size=size,
        gender=gender,
        age=age,
        season=season,
        color_series=color_series,
        category_l2=category_l2,
        series=series,
        role=role,
        up_time_since=since_key,
    )
    return {
        "offset": max(0, int(offset)),
        "size": max(1, int(size)),
        "filters": {
            "gender": gender or [],
            "age": age or [],
            "season": season or [],
            "color_series": color_series or [],
            "category_l2": category_l2 or [],
            "series": series or [],
            "role": role or [],
            "up_time_since": since_key or "",
        },
        "total": total,
        "skus": rows,
    }


@app.get("/api/skus/facets")
def sku_facets() -> dict:
    """返回商品浏览侧栏所需的筛选项：固定枚举（季/性/龄/色系）+ 数据驱动分面（类目/系列）。"""
    agg = _svc._data.sku_facets()  # noqa: SLF001
    return {
        "season": sorted(SEASON_CANONICAL),
        "gender": sorted(GENDER_CANONICAL),
        "age": ["成人", "小童", "中大童", "婴幼童", "通码"],
        "color_series": sorted(COLOR_SERIES_BASE_VALUES),
        "category_l2": agg.get("category_l2", []),
        "series": agg.get("series", []),
    }


@app.get("/api/outfits/search")
def search_outfits(q: str, size: int = 30) -> dict:
    rows = _svc._data.search_outfits_text(q, size)  # noqa: SLF001
    return {"query": q, "outfits": rows}


@app.get("/api/outfits/by-sku/{sku_id}")
def outfits_by_sku(sku_id: str, size: int = 100) -> dict:
    sid = (sku_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="need sku_id")
    rows = _svc._data.outfits_by_sku(sid, size=size)  # noqa: SLF001
    from backend.retrieval.data_facade import DataFacade
    rows = DataFacade._enrich_outfit_color_tags(rows)  # noqa: SLF001
    return {"sku_id": sid, "total": len(rows), "outfits": rows}


@app.post("/api/outfits/mget")
def mget_outfits(body: OutfitMgetBody) -> dict:
    rows = _svc._data.mget_outfits(body.outfit_ids)  # noqa: SLF001
    return {"outfits": rows}


@app.get("/outfits/{outfit_id}")
def get_outfit(outfit_id: str) -> dict:
    row = _svc._data.get_outfit(outfit_id)  # noqa: SLF001
    if not row:
        raise HTTPException(status_code=404, detail="outfit not found")
    return row


@app.get("/debug/images")
def debug_images(
    spu_id: Optional[str] = None,
    sku_id: Optional[str] = None,
):
    if sku_id:
        row = _svc._data.get_sku(sku_id)  # noqa: SLF001
        if not row:
            raise HTTPException(status_code=404, detail="sku not found")
        return row
    if spu_id:
        return get_spu_skus(spu_id)
    raise HTTPException(
        status_code=400,
        detail="need spu_id or sku_id",
    )


@app.get("/eval/review_detail.html")
def eval_detail_page():
    fp = _eval / "review_detail.html"
    if not fp.is_file():
        raise HTTPException(status_code=404, detail="review_detail.html not found")
    return FileResponse(fp)


class ReviewBody(BaseModel):
    data_file: str
    input_sku_id: str
    outfit_id: str
    rating: Optional[int] = None
    comment: Optional[str] = None
    reviewer: Optional[str] = None
    reviewer_role: Optional[str] = None
    reviewer_name: Optional[str] = None


def _review_store():
    return get_review_store()


@app.get("/eval/api/reviews")
def list_reviews(file: str):
    try:
        return _review_store().get(file)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/eval/api/reviews")
def save_review(body: ReviewBody):
    try:
        return _review_store().add(
            data_file=body.data_file,
            input_sku_id=body.input_sku_id,
            outfit_id=body.outfit_id,
            rating=body.rating,
            comment=body.comment,
            reviewer=body.reviewer,
            reviewer_role=body.reviewer_role,
            reviewer_name=body.reviewer_name,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.delete("/eval/api/reviews")
def remove_review(id: str):
    try:
        ok = _review_store().delete(id)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="review not found")
    return {"deleted": True}


@app.get("/eval/api/runs")
def list_runs():
    results_dir = _eval / "results"
    if not results_dir.is_dir():
        return []
    runs = []
    for d in sorted(results_dir.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        index_file = d / "eval_results.json"
        if not index_file.is_file():
            continue
        try:
            data = json.loads(index_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        runs.append({
            "ts": d.name,
            "eval_time": data.get("eval_time", ""),
            "total_skus": data.get("total_skus", 0),
            "success_count": data.get("success_count", 0),
            "error_count": data.get("error_count", 0),
            "categories": data.get("categories", []),
        })
    return runs


if _eval.is_dir():
    app.mount("/eval", StaticFiles(directory=str(_eval), html=True), name="eval")

# ── 检索调试 API ──


class AnnSearchRequest(BaseModel):
    image_base64: str
    image_content_type: str = "image/jpeg"
    top_k: int = 10


@app.get("/api/search-debug/ann/status")
def api_ann_status() -> dict:
    return get_ann_status()


@app.post("/api/search-debug/ann/search")
async def api_ann_search(body: AnnSearchRequest) -> dict:
    if body.top_k < 1 or body.top_k > 100:
        raise HTTPException(status_code=400, detail="top_k 应在 1~100")
    if not body.image_base64:
        raise HTTPException(status_code=400, detail="image_base64 不能为空")
    try:
        return await search_neighbors(
            image_base64=body.image_base64,
            image_content_type=body.image_content_type,
            top_k=body.top_k,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/api/search-debug/es/config")
def api_es_config() -> dict:
    return get_es_config()


class EsSearchRequest(BaseModel):
    q: str = ""
    index: str = ""
    size: int = 20


@app.post("/api/search-debug/es/search")
def api_es_search(body: EsSearchRequest) -> dict:
    try:
        return search_es_direct(q=body.q, index=body.index, size=body.size)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/search-debug/es/search-smart")
async def api_es_search_smart(body: EsSearchRequest) -> dict:
    try:
        return await search_es_smart(q=body.q, index=body.index, size=body.size)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/search-debug/milvus/config")
def api_milvus_config() -> dict:
    return get_milvus_config()


class MilvusHybridDebugRequest(BaseModel):
    query: str
    top_k: int = 20
    kw_w: Optional[float] = None
    sem_w: Optional[float] = None
    ranker: str = "rrf"
    expr: Optional[str] = None
    skip_rewrite: bool = True
    output_fields: Optional[List[str]] = None


@app.post("/api/search-debug/milvus/hybrid-search")
async def api_milvus_hybrid_search(body: MilvusHybridDebugRequest) -> dict:
    if not (body.query or "").strip():
        raise HTTPException(status_code=400, detail="query 不能为空")
    if body.top_k < 1 or body.top_k > 100:
        raise HTTPException(status_code=400, detail="top_k 应在 1~100")
    if body.kw_w is not None and body.kw_w < 0:
        raise HTTPException(status_code=400, detail="kw_w 不能为负")
    if body.sem_w is not None and body.sem_w < 0:
        raise HTTPException(status_code=400, detail="sem_w 不能为负")
    if body.ranker not in ("rrf", "weighted"):
        raise HTTPException(status_code=400, detail="ranker 仅支持 rrf / weighted")
    try:
        return await milvus_hybrid_debug(
            query=body.query, top_k=body.top_k, kw_w=body.kw_w, sem_w=body.sem_w,
            ranker=body.ranker, expr=body.expr or None,
            skip_rewrite=body.skip_rewrite, output_fields=body.output_fields,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# 检索调试页面
_search_debug_web = _ROOT / "web"


@app.get("/search-debug")
def search_debug_page():
    """检索调试台页面。"""
    fp = _web / "search-debug.html"
    if fp.is_file():
        return FileResponse(fp)
    raise HTTPException(status_code=404, detail="search-debug.html not found")
