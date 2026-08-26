"""Milvus 向量检索（新 MilvusClient API，BM25 + dense hybrid）。

索引构建（见 ``scripts/build_text_milvus_index.py``）：``fila_sku_text_vectors``
集合在 cloud 模式下挂 ``search_text``（VARCHAR，enable_analyzer=chinese）→ BM25
``Function`` → 自动生成 ``sparse_vector``；dense ``text_vector`` 保留 HNSW/COSINE。

检索：``sku_text_vectors`` 走 ``hybrid_search``（sparse BM25 原文查询 + dense 向量 +
``WeightedRanker``/``RRFRanker``）；其余集合（image / complementary）dense-only。
运行时检测集合是否含 ``sparse_vector`` 字段：有→hybrid，无→dense-only 降级，
从而支持零停机滚动（先部署代码→再重建集合→自动切 hybrid）。本地 *.db（Lite）
不支持 BM25，强制 dense-only。
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

from backend.config import (
    get_milvus_token,
    get_milvus_uri,
    is_milvus_lite_local_uri,
    load_config,
    restore_stashed_milvus_uri,
    stash_milvus_db_uri_before_pymilvus_import,
)

logger = logging.getLogger(__name__)

# pymilvus import 时读取 MILVUS_URI；本地 *.db 需先暂存再还原。
_cfg0 = load_config()
_mv0 = _cfg0.get("milvus") or {}
_uri_env0 = str(_mv0.get("uri_env") or "FILA_MILVUS_URI")
stash_milvus_db_uri_before_pymilvus_import(_uri_env0)
try:
    from pymilvus import (
        AnnSearchRequest,
        MilvusClient as _PymilvusClient,
        RRFRanker,
        WeightedRanker,
    )
except ImportError:  # pragma: no cover
    _PymilvusClient = None  # type: ignore
    AnnSearchRequest = None  # type: ignore
    RRFRanker = None  # type: ignore
    WeightedRanker = None  # type: ignore
finally:
    restore_stashed_milvus_uri()


class MilvusClient:
    """统一 Milvus 检索门面（新 MilvusClient API）。

    公开方法签名与旧 ORM 版本兼容，调用方（sku_retriever / complementary_recall）
    无需改动；``search_sku_text_vectors`` 新增可选 ``query_text`` 参数以启用 hybrid。
    """

    def __init__(self) -> None:
        self._reload()

    def _reload(self) -> None:
        cfg = load_config()
        mv = cfg.get("milvus") or {}
        self._enabled = bool(mv.get("enabled"))
        self._collections = mv.get("collections") or {}
        self._vector_field = mv.get("vector_field") or "product_vector"
        self._text_vector_field = mv.get("text_vector_field") or "text_vector"
        self._metric = mv.get("metric_type") or "COSINE"
        sp = mv.get("search_params") or {}
        self._ef = int(sp.get("ef") or 64)
        self._nprobe = int(sp.get("nprobe") or 16)
        self._client: Any = None
        self._uri = get_milvus_uri(cfg)
        self._token = get_milvus_token(cfg)
        self._lite = is_milvus_lite_local_uri(self._uri)

        th = mv.get("text_hybrid") or {}
        self._hybrid_enabled = bool(th.get("enabled", True))
        self._search_text_field = str(th.get("search_text_field") or "search_text")
        self._sparse_field = str(th.get("sparse_vector_field") or "sparse_vector")
        self._ranker_kind = str(th.get("ranker") or "weighted").lower()
        self._kw_weight = float(th.get("keyword_weight") if th.get("keyword_weight") is not None else 0.2)
        self._sem_weight = float(th.get("semantic_weight") if th.get("semantic_weight") is not None else 0.8)
        self._rrf_k = int(th.get("rrf_k") or 60)
        # 集合字段缓存：name -> set(field_name)，用于判断是否具备 hybrid 能力
        self._field_cache: dict[str, set[str]] = {}

    @property
    def _dense_search_params(self) -> dict:
        metric = (self._metric or "COSINE").strip().upper()
        if self._lite:
            return {"metric_type": metric, "params": {"nprobe": self._nprobe}}
        return {"metric_type": metric, "params": {"ef": self._ef}}

    def hit_to_similarity(self, raw: float) -> float:
        """将 search 返回的 hit.distance 转为越大越相似的 similarity。

        Milvus 文档：COSINE / IP 下返回值越大越相似（与 L2 相反）。
        L2 下 distance 越小越近，这里映射为 1/(1+d) 便于与 COSINE 同向比较。
        hybrid（WeightedRanker norm_score）返回归一化分数，按 COSINE 同向处理。
        """
        m = (self._metric or "COSINE").strip().upper()
        r = float(raw)
        if m in ("COSINE", "IP", "INNER_PRODUCT"):
            return r
        if m in ("L2", "EUCLIDEAN"):
            return 1.0 / (1.0 + max(0.0, r))
        return max(0.0, min(1.0, 1.0 - r))

    def _ensure(self) -> Any:
        if not self._enabled or _PymilvusClient is None:
            return None
        if self._client is not None:
            return self._client
        if not self._uri:
            return None
        try:
            self._client = _PymilvusClient(uri=self._uri, token=self._token or None)
            return self._client
        except Exception as e:  # noqa: BLE001
            logger.warning("milvus connect failed: %s", e)
            return None

    def _collection_fields(self, client: Any, name: str) -> set[str]:
        if name in self._field_cache:
            return self._field_cache[name]
        fields: set[str] = set()
        try:
            desc = client.describe_collection(name)
            for f in desc.get("fields", []) or []:
                fn = f.get("name")
                if fn:
                    fields.add(fn)
        except Exception:  # noqa: BLE001
            pass
        self._field_cache[name] = fields
        return fields

    def _supports_hybrid(self, client: Any, name: str) -> bool:
        """集合是否具备 sparse_vector 字段（即已按 hybrid schema 重建过）。"""
        if self._lite or not self._hybrid_enabled:
            return False
        return self._sparse_field in self._collection_fields(client, name)

    def _build_ranker(self) -> Any:
        if self._ranker_kind == "rrf":
            return RRFRanker(k=self._rrf_k)
        return WeightedRanker(self._kw_weight, self._sem_weight)

    def search_vector_collection(
        self,
        collection_name: str,
        vector: List[float],
        top_k: int,
        id_field: str,
        expr: Optional[str] = None,
        *,
        vector_field: Optional[str] = None,
        group_by_field: Optional[str] = None,
    ) -> List[Tuple[str, float]]:
        """dense-only 近邻检索（image / complementary / text 降级路径共用）。"""
        client = self._ensure()
        if client is None:
            return []
        anns_field = vector_field or self._vector_field
        try:
            if not client.has_collection(collection_name):
                return []
            output_fields = [id_field]
            search_kwargs: dict[str, Any] = {
                "collection_name": collection_name,
                "data": [vector],
                "anns_field": anns_field,
                "search_params": self._dense_search_params,
                "limit": top_k,
                "filter": expr or "",
                "output_fields": output_fields,
            }
            if group_by_field:
                if group_by_field not in output_fields:
                    output_fields.append(group_by_field)
                search_kwargs["group_by_field"] = group_by_field
            res = client.search(**search_kwargs)
            return self._extract_pairs(res, id_field)
        except Exception as e:  # noqa: BLE001
            logger.warning("milvus search: %s", e)
            return []

    def _extract_pairs(self, res: Any, id_field: str) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        if not res:
            return out
        hits = res[0] if isinstance(res, list) else []
        for hit in hits:
            ent = hit.get("entity", {}) if isinstance(hit, dict) else {}
            eid = ent.get(id_field)
            if eid:
                out.append((str(eid), float(hit.get("distance", 0) if isinstance(hit, dict) else 0)))
        return out

    def search_sku_vectors(
        self,
        vector: List[float],
        top_k: int,
        expr: Optional[str] = None,
    ) -> List[Tuple[str, float]]:
        name = self._collections.get("sku_vectors") or "fila_sku_vectors"
        return self.search_vector_collection(
            name, vector, top_k, "sku_id", expr=expr,
            group_by_field="sku_id",
        )

    def search_sku_text_vectors(
        self,
        vector: List[float],
        top_k: int,
        expr: Optional[str] = None,
        *,
        query_text: Optional[str] = None,
    ) -> List[Tuple[str, float]]:
        """sku_text_vectors 检索：集合含 sparse_vector 时走 hybrid，否则 dense-only。

        ``query_text``：原始查询文本（关键词），喂给 BM25 sparse 子检索；
        缺省或集合不支持 hybrid 时退化为 dense-only（``text_vector``）。
        """
        name = self._collections.get("sku_text_vectors") or "fila_sku_text_vectors"
        client = self._ensure()
        if client is None:
            return []
        try:
            if not client.has_collection(name):
                return []
            if query_text and self._supports_hybrid(client, name):
                return self._hybrid_search_text(client, name, query_text, vector, top_k, expr)
            return self.search_vector_collection(
                name, vector, top_k, "sku_id", expr=expr,
                vector_field=self._text_vector_field,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("milvus text search: %s", e)
            return []

    def _hybrid_search_text(
        self,
        client: Any,
        name: str,
        query_text: str,
        vector: List[float],
        top_k: int,
        expr: Optional[str],
    ) -> list[tuple[str, float]]:
        sparse_req = AnnSearchRequest(
            data=[query_text],
            anns_field=self._sparse_field,
            param={"metric_type": "BM25"},
            limit=top_k,
            expr=expr or None,
        )
        dense_req = AnnSearchRequest(
            data=[vector],
            anns_field=self._text_vector_field,
            param=self._dense_search_params,
            limit=top_k,
            expr=expr or None,
        )
        res = client.hybrid_search(
            collection_name=name,
            reqs=[sparse_req, dense_req],
            ranker=self._build_ranker(),
            limit=top_k,
            output_fields=["sku_id"],
        )
        return self._extract_pairs(res, "sku_id")

    def search_sku_complementary_vectors(
        self,
        vector: List[float],
        top_k: int,
        expr: Optional[str] = None,
    ) -> List[Tuple[str, float]]:
        """Search complementary item embeddings generated by outfit-transformer."""
        name = (
            self._collections.get("sku_complementary_vectors")
            or "fila_sku_complementary_vectors"
        )
        return self.search_vector_collection(
            name,
            vector,
            top_k,
            "sku_id",
            expr=expr,
            vector_field="complementary_vector",
        )
