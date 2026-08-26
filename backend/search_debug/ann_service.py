"""Milvus ANN 向量检索调试服务。

支持两种模式：
- 单索引模式：使用 config.yaml 中已有的 ``embedding`` + ``milvus`` 配置
- 双索引模式：使用 config.yaml 中 ``search_debug.ann.dual_index`` 配置
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from functools import partial
from pathlib import Path
from typing import Any, Optional

from backend.config import (
    get_milvus_token,
    get_milvus_uri,
    get_root,
    is_milvus_lite_local_uri,
    load_config,
    restore_stashed_milvus_uri,
    stash_milvus_db_uri_before_pymilvus_import,
)
from backend.search_debug.doubao_embed import embed_image_url as embed_doubao_image
from backend.search_debug.qwen3_vl_embed import (
    embed_image_url as embed_qwen3_image,
    fetch_served_model_id,
)

logger = logging.getLogger(__name__)

_qwen3_model_cache: dict[str, str] = {}


def _resolve_qwen3_model_id(emb_cfg: dict[str, Any]) -> str:
    model = str(emb_cfg.get("model") or "").strip()
    if model:
        return model
    base_url = str(emb_cfg.get("base_url") or "").strip()
    api_key = str(emb_cfg.get("api_key") or "")
    timeout = float(emb_cfg.get("timeout_sec") or 120)
    cache_key = f"{base_url}\n{api_key}"
    cached = _qwen3_model_cache.get(cache_key)
    if cached:
        return cached
    mid = fetch_served_model_id(
        base_url,
        api_key=api_key,
        timeout_sec=timeout,
    )
    _qwen3_model_cache[cache_key] = mid
    return mid


def _embedding_model_label(emb_cfg: dict[str, Any]) -> str:
    prov = str(emb_cfg.get("provider") or "").strip().lower()
    if prov in ("qwen3_vl", "qwen3-vl", "qwen3"):
        m = str(emb_cfg.get("model") or "").strip()
        return m or _resolve_qwen3_model_id(emb_cfg)
    return str(emb_cfg.get("model") or "").strip()


def _embed_image_sync(emb_cfg: dict[str, Any], image_url: str) -> Optional[list[float]]:
    prov = str(emb_cfg.get("provider") or "").strip().lower()
    if prov in ("qwen3_vl", "qwen3-vl", "qwen3"):
        base_url = str(emb_cfg.get("base_url") or "").strip()
        if not base_url:
            return None
        dim = int(emb_cfg.get("dimensions") or 4096)
        model = _resolve_qwen3_model_id(emb_cfg)
        api_key = str(emb_cfg.get("api_key") or "")
        timeout = float(emb_cfg.get("timeout_sec") or 120)
        return embed_qwen3_image(
            base_url,
            model,
            image_url,
            text="",
            api_key=api_key,
            timeout_sec=timeout,
            expected_dim=dim,
        )
    if prov in ("ark", "doubao", "volc", "doubao_vision"):
        return embed_doubao_image(emb_cfg, image_url)
    raise ValueError(f"未知 embedding.provider: {prov!r}")


def _resolve_db_path(entry: dict[str, Any]) -> Path:
    """解析 Milvus 本地库路径（相对 fila_agent_html/ 根目录）。"""
    rel = str(entry.get("local_data_file") or "").strip()
    if not rel:
        mv = entry.get("milvus") or {}
        rel = str(mv.get("local_data_file") or "").strip()
    if not rel:
        raise ValueError("Milvus 索引项缺少 local_data_file")
    p = (get_root() / rel).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _build_single_index_entry(cfg: dict[str, Any]) -> dict[str, Any]:
    """从主配置构造单索引模式的索引条目。"""
    emb = dict(cfg.get("embedding") or {})
    mv = dict(cfg.get("milvus") or {})
    entry: dict[str, Any] = {
        "id": "primary",
        "label": _embedding_model_label(emb),
        "embedding": emb,
        "collection": str(mv.get("collections", {}).get("sku_vectors") or "fila_sku_vectors"),
        "vector_field": str(mv.get("vector_field") or "product_vector"),
        "metric_type": str(mv.get("metric_type") or "COSINE"),
    }
    # 本地模式用 local_data_file，云端模式用 uri
    mode = (os.environ.get(str(mv.get("mode_env") or "FILA_MILVUS_MODE")) or str(mv.get("mode") or "local")).strip().lower()
    if mode == "local":
        rel = str(mv.get("local_data_file") or "").strip()
        if rel:
            entry["local_data_file"] = rel
    return entry


def _normalize_dual_indexes(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 search_debug.ann.dual_index 配置。"""
    dual = (cfg.get("search_debug") or {}).get("ann", {}).get("dual_index") or {}
    if not dual.get("enabled"):
        return []
    raw = dual.get("indexes") or []
    if not isinstance(raw, list) or len(raw) != 2:
        raise ValueError("search_debug.ann.dual_index.indexes 必须恰好配置 2 条")
    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        ent = dict(item)
        ent.setdefault("id", f"dual_{i}")
        ent.setdefault("label", f"双索引 {i + 1}")
        mv = ent.get("milvus") or {}
        if mv.get("local_data_file") and not str(ent.get("local_data_file") or "").strip():
            ent["local_data_file"] = str(mv.get("local_data_file") or "").strip()
        if mv.get("collection") and not str(ent.get("collection") or "").strip():
            ent["collection"] = str(mv.get("collection") or "").strip()
        if mv.get("vector_field"):
            ent.setdefault("vector_field", str(mv.get("vector_field")))
        if mv.get("metric_type"):
            ent.setdefault("metric_type", str(mv.get("metric_type")))
        ent.setdefault("vector_field", "product_vector")
        ent.setdefault("metric_type", "COSINE")
        emb = ent.get("embedding")
        if not isinstance(emb, dict) or not emb:
            raise ValueError(f"dual_index.indexes[{i}] 缺少 embedding 配置块")
        ent["embedding"] = dict(emb)
        out.append(ent)
    if len(out) != 2:
        raise ValueError(
            f"dual_index.indexes 必须恰好配置 2 条，当前 {len(out)}",
        )
    return out


class NeighborItem:
    def __init__(self, sku_id: str, product_name: str = "", product_image: str = "",
                 distance: float = 0.0, detail_url: str = ""):
        self.sku_id = sku_id
        self.product_name = product_name
        self.product_image = product_image
        self.distance = distance
        self.detail_url = detail_url

    def to_dict(self) -> dict[str, Any]:
        return {
            "sku_id": self.sku_id,
            "product_name": self.product_name,
            "product_image": self.product_image,
            "distance": self.distance,
            "detail_url": self.detail_url,
        }


class IndexNeighbors:
    def __init__(self, id: str, label: str, embedding_provider: str = "",
                 embedding_model: str = "", neighbors: list[NeighborItem] | None = None,
                 error: str = ""):
        self.id = id
        self.label = label
        self.embedding_provider = embedding_provider
        self.embedding_model = embedding_model
        self.neighbors = neighbors or []
        self.error = error

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "embedding_provider": self.embedding_provider,
            "embedding_model": self.embedding_model,
            "neighbors": [n.to_dict() for n in self.neighbors],
            "error": self.error,
        }


def _hits_from_search(res: Any, detail_base: str) -> list[NeighborItem]:
    hits: list[NeighborItem] = []
    if not res or not res[0]:
        return hits
    for hit in res[0]:
        sid = str(hit.entity.get("sku_id") or "")
        if not sid:
            continue
        detail_url = ""
        if detail_base:
            sep = "&" if "?" in detail_base else "?"
            detail_url = f"{detail_base}{sep}sku={sid}"
        hits.append(
            NeighborItem(
                sku_id=sid,
                product_name=str(hit.entity.get("product_name") or ""),
                product_image=str(hit.entity.get("product_image") or ""),
                distance=float(hit.distance),
                detail_url=detail_url,
            ),
        )
    return hits


class AnnSearchService:
    """ANN 检索服务，管理 Milvus 连接生命周期。"""

    def __init__(self):
        self._slots: list[dict[str, Any]] = []
        self._initialized = False

    def init(self, app=None) -> None:
        """初始化 Milvus 连接。先尝试单索引，再尝试双索引。"""
        cfg = load_config()
        entries: list[dict[str, Any]] = []

        # 双索引模式
        try:
            dual = _normalize_dual_indexes(cfg)
            if dual:
                entries = dual
                logger.info("ANN 检索调试：双索引模式，共 %d 个索引", len(entries))
        except Exception as exc:
            logger.warning("双索引配置解析失败，回退单索引: %s", exc)

        # 单索引模式（fallback 或默认）
        if not entries:
            try:
                entries = [_build_single_index_entry(cfg)]
                logger.info("ANN 检索调试：单索引模式")
            except Exception as exc:
                logger.warning("无法构建单索引配置: %s", exc)
                return

        # 连接 Milvus
        self._connect_slots(entries)

    def _connect_slots(self, entries: list[dict[str, Any]]) -> None:
        """连接所有 Milvus 索引。"""
        from pymilvus import Collection, connections

        self._slots = []
        for i, ent in enumerate(entries):
            alias = f"search_debug_{i}"
            col_name = str(ent.get("collection") or "").strip()
            if not col_name:
                raise ValueError(f"索引 [{i}] 缺少 collection")

            # 确定连接方式
            if "local_data_file" in ent and ent["local_data_file"]:
                uri = str(_resolve_db_path(ent))
                is_local = True
            else:
                uri = get_milvus_uri()
                is_local = is_milvus_lite_local_uri(uri)

            token = get_milvus_token() if not is_local else ""

            vf = str(ent.get("vector_field") or "product_vector")
            metric = str(ent.get("metric_type") or "COSINE")

            if is_local:
                os.environ["MILVUS_URI"] = uri
                stash_milvus_db_uri_before_pymilvus_import(env_key="MILVUS_URI")
                try:
                    from pymilvus import Collection as _Col, connections as _conns
                finally:
                    restore_stashed_milvus_uri()

                _conns.connect(alias, uri=uri, token=token)
                col = _Col(col_name, using=alias)
            else:
                connections.connect(alias, uri=uri, token=token)
                col = Collection(col_name, using=alias)

            col.load()

            self._slots.append({
                "alias": alias,
                "col": col,
                "vf": vf,
                "metric": metric,
                "lock": asyncio.Lock(),
                "id": str(ent.get("id") or f"index_{i}"),
                "label": str(ent.get("label") or f"索引 {i + 1}"),
                "uri": uri,
                "collection": col_name,
                "embedding": dict(ent.get("embedding") or {}),
            })
            logger.info(
                "ANN[%s] alias=%s uri=%s collection=%s",
                i, alias, uri, col_name,
            )

        self._initialized = True

    def shutdown(self) -> None:
        """断开所有 Milvus 连接。"""
        try:
            from pymilvus import connections
        except ImportError:
            return
        for s in self._slots:
            try:
                connections.disconnect(s["alias"])
            except Exception:
                pass
        self._slots = []
        self._initialized = False
        logger.info("ANN 检索调试：Milvus 连接已全部关闭")

    def get_status(self) -> dict[str, Any]:
        return {
            "initialized": self._initialized,
            "index_count": len(self._slots),
            "indexes": [
                {
                    "id": s.get("id"),
                    "label": s.get("label"),
                    "uri": s.get("uri"),
                    "collection": s.get("collection"),
                }
                for s in self._slots
            ],
        }

    async def search(self, *, image_base64: str, image_content_type: str,
                     top_k: int = 10) -> dict[str, Any]:
        """执行向量检索。

        Returns:
            {"model": str, "top_k": int, "indexes": [IndexNeighbors.to_dict(), ...]}
        """
        # 详情页用同源相对路径（与 outfits-viewer/browse.js 一致），
        # 不再依赖已废弃的 recommend.outfits_viewer_base。
        detail_base = "/outfits-viewer/detail.html"

        if not self._slots:
            raise RuntimeError("Milvus 索引未初始化")

        data_uri = f"data:{image_content_type};base64,{image_base64}"

        # 并行嵌入
        embed_tasks = []
        for slot in self._slots:
            emb_cfg = slot["embedding"]
            embed_tasks.append(
                asyncio.to_thread(_embed_image_sync, emb_cfg, data_uri),
            )
        vectors: list[Optional[list[float]]] = list(await asyncio.gather(*embed_tasks))

        # 并行检索
        search_tasks = []
        for slot, vec in zip(self._slots, vectors):
            emb_cfg = slot["embedding"]
            if vec is None:
                search_tasks.append(
                    _failed_result(slot, emb_cfg, "嵌入失败、维度不匹配或缺少密钥/基址"),
                )
                continue
            search_tasks.append(
                self._search_one_slot(slot, vec, top_k, detail_base, emb_cfg),
            )

        index_results = await asyncio.gather(*search_tasks)
        models = [x.embedding_model for x in index_results if x.embedding_model]
        summary = ",".join(models) if models else ""

        return {
            "model": summary,
            "top_k": top_k,
            "indexes": [x.to_dict() for x in index_results],
        }

    async def _search_one_slot(
        self,
        slot: dict[str, Any],
        vec: list[float],
        top_k: int,
        detail_base: str,
        emb_cfg: dict[str, Any],
    ) -> IndexNeighbors:
        col = slot["col"]
        vf = slot["vf"]
        metric = slot["metric"]
        lock: asyncio.Lock = slot["lock"]
        idx_id = str(slot["id"])
        label = str(slot["label"])
        prov = str(emb_cfg.get("provider") or "")
        emb_model = _embedding_model_label(emb_cfg)
        search_fn = partial(
            col.search,
            data=[vec],
            anns_field=vf,
            param={"metric_type": metric, "params": {"nprobe": 16}},
            limit=top_k,
            output_fields=["sku_id", "product_image", "product_name"],
        )
        try:
            async with lock:
                res = await asyncio.to_thread(search_fn)
            hits = _hits_from_search(res, detail_base)
            return IndexNeighbors(
                id=idx_id,
                label=label,
                embedding_provider=prov,
                embedding_model=emb_model,
                neighbors=hits,
                error="",
            )
        except Exception as exc:
            logger.exception("Milvus 检索失败: %s", idx_id)
            return IndexNeighbors(
                id=idx_id,
                label=label,
                embedding_provider=prov,
                embedding_model=emb_model,
                neighbors=[],
                error=str(exc),
            )


async def _failed_result(slot: dict[str, Any], emb_cfg: dict[str, Any],
                         error_msg: str) -> IndexNeighbors:
    return IndexNeighbors(
        id=str(slot["id"]),
        label=str(slot["label"]),
        embedding_provider=str(emb_cfg.get("provider") or ""),
        embedding_model=_embedding_model_label(emb_cfg),
        neighbors=[],
        error=error_msg,
    )


# 全局单例
_ann_service: Optional[AnnSearchService] = None


def init_ann_search(app=None) -> AnnSearchService:
    global _ann_service
    if _ann_service is None:
        _ann_service = AnnSearchService()
        _ann_service.init(app)
    return _ann_service


def get_ann_status() -> dict[str, Any]:
    if _ann_service is None:
        return {"initialized": False, "index_count": 0, "indexes": []}
    return _ann_service.get_status()


async def search_neighbors(*, image_base64: str, image_content_type: str,
                           top_k: int = 10) -> dict[str, Any]:
    if _ann_service is None:
        raise RuntimeError("ANN 服务未初始化")
    return await _ann_service.search(
        image_base64=image_base64,
        image_content_type=image_content_type,
        top_k=top_k,
    )


def shutdown_ann() -> None:
    global _ann_service
    if _ann_service is not None:
        _ann_service.shutdown()
        _ann_service = None