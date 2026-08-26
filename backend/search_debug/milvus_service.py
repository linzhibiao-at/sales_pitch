"""Milvus hybrid 检索调试服务：关键词(BM25) / 语义(dense) / 混合(hybrid) 三路对比。

包装 backend/retrieval/hybrid_search.py 的 FilaSkuHybridSearcher，
单次调用返回三路结果 + 可选查询改写预览，每路独立计时与错误捕获。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from backend.config import (
    get_milvus_uri,
    is_milvus_lite_local_uri,
    load_config,
)
from backend.retrieval.hybrid_search import (
    DEFAULT_OUTPUT_FIELDS,
    FilaSkuHybridSearcher,
    rewrite_query,
)

logger = logging.getLogger(__name__)

_searcher: Optional[FilaSkuHybridSearcher] = None
_facade: Optional[Any] = None


def _to_jsonable(value: Any) -> Any:
    """把 Milvus 返回的 protobuf/numpy 等非 JSON 原生类型归一化。

    DEFAULT_OUTPUT_FIELDS 含数组字段（features/selling_point_label 等），
    Milvus 返回 google._upb._message.RepeatedScalarContainer，FastAPI 无法
    直接 JSON 序列化，这里统一转成 list/标量。生产通路只取 sku_id 不触发，
    仅调试 tab 取全字段时需要。
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    # protobuf RepeatedScalarContainer 可被 list()/for 消费，但 C 扩展不暴露
    # __iter__ 属性（hasattr 为 False），故用 try 而非 hasattr 检测。
    try:
        return [_to_jsonable(v) for v in value]
    except TypeError:
        pass
    if hasattr(value, "item") and callable(value.item):  # numpy 标量
        try:
            return value.item()
        except Exception:
            pass
    return value


def _normalize_hits(hits: list[dict]) -> list[dict]:
    return [{k: _to_jsonable(v) for k, v in h.items()} for h in hits] if hits else []


def _get_searcher() -> FilaSkuHybridSearcher:
    global _searcher
    if _searcher is None:
        _searcher = FilaSkuHybridSearcher()
    return _searcher


def _get_facade() -> Any:
    """惰性 DataFacade 单例：Milvus 集合无图片字段，tryon_image 需从 ES 补全。"""
    global _facade
    if _facade is None:
        from backend.retrieval.data_facade import DataFacade
        _facade = DataFacade()
    return _facade


def _pick_tryon_image(src: dict) -> str:
    """从 ES sku _source 取展示图：tryon_image 优先，回退 display_image/index_image。"""
    for k in ("tryon_image", "display_image", "index_image"):
        v = str(src.get(k) or "").strip()
        if v:
            return v
    return ""


def _fetch_tryon_image_map(sku_ids: list[str]) -> dict[str, str]:
    """按 sku_id 批量取 tryon_image（ES 不可用或单条未命中返回空）。"""
    ids = [str(x).strip() for x in sku_ids if str(x).strip()]
    if not ids:
        return {}
    try:
        rows = _get_facade().get_skus(ids)
    except Exception as exc:  # ES 故障不阻塞检索结果
        logger.warning("milvus debug: 取 tryon_image 失败: %s", exc)
        return {}
    out: dict[str, str] = {}
    for src in rows or []:
        sid = str(src.get("sku_id") or "").strip()
        if sid:
            out[sid] = _pick_tryon_image(src)
    return out


def _enrich_with_tryon_image(branches: list[dict[str, Any]], img_map: dict[str, str]) -> None:
    for b in branches:
        for h in b.get("hits") or []:
            h["tryon_image"] = img_map.get(str(h.get("sku_id") or "").strip(), "")


def get_milvus_config() -> dict[str, Any]:
    """返回 hybrid 检索调试配置（不连客户端，安全可调）。"""
    s = _get_searcher()
    cfg = load_config()
    uri = get_milvus_uri(cfg) or ""
    hybrid_supported = bool(uri) and not is_milvus_lite_local_uri(uri)
    return {
        "collection": s.collection_name,
        "ranker": s._ranker,
        "keyword_weight": s._kw_w,
        "semantic_weight": s._sem_w,
        "default_limit": s._limit,
        "nprobe": s._nprobe,
        "output_fields": list(DEFAULT_OUTPUT_FIELDS),
        "hybrid_supported": hybrid_supported,
    }


async def milvus_hybrid_debug(
    query: str,
    *,
    top_k: int = 20,
    kw_w: Optional[float] = None,
    sem_w: Optional[float] = None,
    ranker: str = "rrf",
    expr: Optional[str] = None,
    skip_rewrite: bool = True,
    output_fields: Optional[list[str]] = None,
    searcher: Optional[FilaSkuHybridSearcher] = None,
) -> dict[str, Any]:
    """三路对比检索。searcher 可注入（测试用），默认走模块单例。"""
    s = searcher or _get_searcher()
    of = output_fields or DEFAULT_OUTPUT_FIELDS
    params = {
        "top_k": top_k, "kw_w": kw_w, "sem_w": sem_w, "ranker": ranker,
        "expr": expr, "skip_rewrite": skip_rewrite,
    }

    rewrite: Optional[dict[str, Any]] = None
    if not skip_rewrite:
        try:
            rw = await asyncio.to_thread(rewrite_query, query, None)
            rewrite = {
                "keyword_query": rw.keyword_query,
                "semantic_query": rw.semantic_query,
                "filters": rw.filters,
                "source": rw.source,
            }
        except Exception as exc:  # 改写失败不阻塞三路
            rewrite = {"error": str(exc)}

    async def _branch(name: str, fn: Any) -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            hits = await asyncio.to_thread(fn)
            return {"hits": _normalize_hits(hits), "took_ms": round((time.perf_counter() - t0) * 1000, 1), "error": None}
        except BaseException as exc:
            logger.error("milvus hybrid debug[%s] 失败: %s", name, exc)
            return {"hits": [], "took_ms": round((time.perf_counter() - t0) * 1000, 1), "error": str(exc)}

    kw_r, sem_r, hyb_r = await asyncio.gather(
        _branch("keyword", lambda: s.search_keyword(
            query, expr=expr, limit=top_k, output_fields=of, skip_rewrite=skip_rewrite)),
        _branch("semantic", lambda: s.search_semantic(
            query, expr=expr, limit=top_k, output_fields=of, skip_rewrite=skip_rewrite)),
        _branch("hybrid", lambda: s.search_hybrid(
            query, expr=expr, limit=top_k, kw_w=kw_w, sem_w=sem_w, ranker=ranker,
            output_fields=of, skip_rewrite=skip_rewrite)),
    )
    # Milvus 集合无图片字段：批量从 ES 补 tryon_image
    all_ids = [str(h.get("sku_id") or "").strip()
               for b in (kw_r, sem_r, hyb_r) for h in (b.get("hits") or [])
               if str(h.get("sku_id") or "").strip()]
    img_map = await asyncio.to_thread(_fetch_tryon_image_map, all_ids)
    _enrich_with_tryon_image([kw_r, sem_r, hyb_r], img_map)
    return {"query": query, "params": params, "rewrite": rewrite,
            "keyword": kw_r, "semantic": sem_r, "hybrid": hyb_r}
