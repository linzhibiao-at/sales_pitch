"""Elasticsearch 可选客户端。"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.config import (
    create_elasticsearch_client,
    env_or_empty,
    get_elasticsearch_hosts,
    get_elasticsearch_indices,
    load_config,
)
from backend.retrieval.up_time_filter import build_up_time_es_filter

logger = logging.getLogger(__name__)

# catalog 侧可见的「运营」搭配来源；合成搭配(text_vector_compose 等合成 source)
# 不在其中，故 browse/search/facet 默认按此过滤可天然排除合成款。
OPERATIONAL_OUTFIT_SOURCES = (
    "cc_material",
    "micro_guide",
    "dphs_outfits",
    "outfits_unique",
)

try:
    from elasticsearch import Elasticsearch
    from elasticsearch.helpers import bulk, scan
except ImportError:  # pragma: no cover
    Elasticsearch = None  # type: ignore
    bulk = None  # type: ignore
    scan = None  # type: ignore


class EsClient:
    def __init__(self) -> None:
        cfg = load_config()
        es = cfg.get("elasticsearch") or {}
        self._enabled = bool(es.get("enabled"))
        self._hosts = get_elasticsearch_hosts(cfg)
        self._indices = get_elasticsearch_indices(cfg)
        self._client: Any = None
        if self._enabled and Elasticsearch:
            user = env_or_empty(str(es.get("username_env") or ""))
            pwd = env_or_empty(str(es.get("password_env") or ""))
            try:
                self._client = create_elasticsearch_client(
                    self._hosts,
                    username=user,
                    password=pwd,
                    timeout_sec=30,
                )
                if not self._client.ping():
                    logger.warning("ES ping failed, disable ES")
                    self._client = None
            except Exception as e:  # noqa: BLE001
                logger.warning("ES init failed: %s", e)
                self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    def search_skus(
        self,
        query: str,
        filters: Optional[Dict[str, Any]],
        size: int,
    ) -> List[str]:
        if not self._client:
            return []
        idx = self._indices["skus"]
        body: dict[str, Any] = {
            "size": size,
            "query": {"match": {"search_text": query}},
        }
        # 上架时间下限：up_time >= config.recommend.up_time_since（与 resolve_es_query_for_role 对齐）
        # 禁用（配置留空）时 build 返回 None，跳过。
        must: list[dict[str, Any]] = [f for f in [build_up_time_es_filter()] if f]
        if filters:
            # 简化：只处理 gender keyword
            g = filters.get("gender")
            if g:
                g_s = str(g).strip()
                if g_s == "儿童":
                    must.append({"terms": {"gender": ["儿童", "男童", "女童"]}})
                elif g_s:
                    must.append({"terms": {"gender": [g_s]}})
        if must:
            body["query"] = {
                "bool": {
                    "must": [{"match": {"search_text": query}}],
                    "filter": must,
                }
            }
        try:
            res = self._client.search(index=idx, body=body)
            hits = res.get("hits", {}).get("hits", [])
            return [h["_source"]["sku_id"] for h in hits if "_source" in h]
        except Exception as e:  # noqa: BLE001
            logger.warning("es search_skus: %s", e)
            return []

    def search_skus_with_query(
        self,
        query_clause: dict[str, Any],
        size: int,
    ) -> list[tuple[str, float]]:
        """按完整 ES query 子句检索 SKU，返回 (sku_id, _score)。"""
        if not self._client or not query_clause:
            return []
        idx = self._indices["skus"]
        body: dict[str, Any] = {
            "size": max(1, int(size)),
            "query": query_clause,
            "_source": ["sku_id"],
        }
        try:
            res = self._client.search(index=idx, body=body)
            hits = res.get("hits", {}).get("hits", [])
            out: list[tuple[str, float]] = []
            for h in hits:
                src = h.get("_source") or {}
                sid = str(src.get("sku_id") or "")
                if not sid:
                    continue
                score = float(h.get("_score") or 0.0)
                out.append((sid, score))
            return out
        except Exception as e:  # noqa: BLE001
            logger.warning("es search_skus_with_query: %s", e)
            return []

    def scan_skus(self, batch_size: int = 2000) -> list[dict[str, Any]]:
        """滚动扫描 skus 索引全量文档，返回 _source 列表（ES 不可用时返回空）。"""
        if not self._client or scan is None:
            return []
        idx = self._indices["skus"]
        try:
            docs = list(
                scan(
                    self._client,
                    index=idx,
                    query={"query": {"match_all": {}}},
                    size=batch_size,
                    _source=True,
                )
            )
            return [
                d["_source"]
                for d in docs
                if isinstance(d.get("_source"), dict)
            ]
        except Exception as e:  # noqa: BLE001
            logger.warning("es scan_skus: %s", e)
            return []

    def get_doc(self, index_key: str, doc_id: str) -> dict[str, Any] | None:
        if not self._client or not doc_id:
            return None
        idx = self._indices[index_key]
        try:
            res = self._client.get(index=idx, id=doc_id)
            if not res.get("found", True):
                return None
            src = res.get("_source") or {}
            return src if isinstance(src, dict) else None
        except Exception as e:  # noqa: BLE001
            logger.warning("es get_doc %s/%s: %s", index_key, doc_id, e)
            return None

    def mget_docs(
        self,
        index_key: str,
        doc_ids: list[str],
    ) -> list[dict[str, Any]]:
        if not self._client or not doc_ids:
            return []
        idx = self._indices[index_key]
        ids = [str(x).strip() for x in doc_ids if str(x).strip()]
        if not ids:
            return []
        try:
            res = self._client.mget(index=idx, body={"ids": ids})
            docs = res.get("docs") or []
            rows: list[dict[str, Any]] = []
            for doc in docs:
                if not doc.get("found", False):
                    continue
                src = doc.get("_source") or {}
                if isinstance(src, dict):
                    rows.append(src)
            return rows
        except Exception as e:  # noqa: BLE001
            logger.warning("es mget_docs %s: %s", index_key, e)
            return []

    def bulk_upsert_docs(
        self,
        index_key: str,
        docs: list[tuple[str, dict[str, Any]]],
    ) -> tuple[int, int]:
        """Bulk index docs by id. Returns (success_count, error_count)."""
        if not self._client or not docs:
            return 0, len(docs)
        if bulk is None:
            logger.warning("elasticsearch.helpers.bulk unavailable")
            return 0, len(docs)
        idx = self._indices[index_key]
        actions = [
            {
                "_op_type": "index",
                "_index": idx,
                "_id": str(doc_id),
                "_source": doc,
            }
            for doc_id, doc in docs
            if str(doc_id).strip() and isinstance(doc, dict)
        ]
        if not actions:
            return 0, 0
        try:
            res = bulk(self._client, actions, raise_on_error=False)
            success = int(res[0]) if isinstance(res, tuple) and res else len(actions)
            errors = res[1] if isinstance(res, tuple) and len(res) > 1 else []
            err_count = len(errors) if errors else 0
            if errors:
                sample = errors[:3]
                logger.warning(
                    "es bulk_upsert_docs %s: %d/%d failed, sample errors: %s",
                    index_key, err_count, len(actions), sample,
                )
            self._client.indices.refresh(index=idx)
            return success, err_count
        except Exception as e:  # noqa: BLE001
            logger.warning("es bulk_upsert_docs %s: %s", index_key, e)
            return 0, len(actions)

    def count_docs(
        self,
        index_key: str,
        query: dict[str, Any],
    ) -> int:
        if not self._client:
            return 0
        idx = self._indices[index_key]
        try:
            res = self._client.count(index=idx, body={"query": query})
            return int(res.get("count") or 0)
        except Exception as e:  # noqa: BLE001
            logger.warning("es count_docs %s: %s", index_key, e)
            return 0

    def delete_docs_by_query(
        self,
        index_key: str,
        query: dict[str, Any],
    ) -> int:
        if not self._client:
            return 0
        idx = self._indices[index_key]
        try:
            res = self._client.delete_by_query(
                index=idx,
                body={"query": query},
                refresh=True,
                conflicts="proceed",
            )
            return int(res.get("deleted") or 0)
        except Exception as e:  # noqa: BLE001
            logger.warning("es delete_docs_by_query %s: %s", index_key, e)
            return 0

    def search_outfits_by_sku(
        self,
        sku_id: str,
        size: int = 100,
        *,
        sources: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not self._client or not sku_id:
            return []
        idx = self._indices["outfits"]
        filters: list[dict[str, Any]] = [{"term": {"sku_ids": sku_id}}]
        source_terms = [str(x).strip() for x in (sources or []) if str(x).strip()]
        if source_terms:
            filters.append({"terms": {"source": source_terms}})
        body = {
            "size": max(1, int(size)),
            "query": {"bool": {"filter": filters}},
        }
        try:
            res = self._client.search(index=idx, body=body)
            hits = res.get("hits", {}).get("hits", [])
            return [
                h.get("_source") or {}
                for h in hits
                if isinstance(h.get("_source"), dict)
            ]
        except Exception as e:  # noqa: BLE001
            logger.warning("es search_outfits_by_sku: %s", e)
            return []

    def expand_spu(self, spu_id: str, size: int = 200) -> list[str]:
        """spu_id -> [sku_id, ...]：按 spu_id 反查 skus 索引返回其下所有 SKU ID。"""
        spu_id = (spu_id or "").strip()
        if not self._client or not spu_id:
            return []
        idx = self._indices["skus"]
        body = {
            "size": max(1, int(size)),
            "query": {"term": {"spu_id": spu_id}},
            "_source": ["sku_id"],
        }
        try:
            res = self._client.search(index=idx, body=body)
            hits = res.get("hits", {}).get("hits", [])
            out: list[str] = []
            for h in hits:
                src = h.get("_source") or {}
                sid = str(src.get("sku_id") or "").strip()
                if sid and sid not in out:
                    out.append(sid)
            return out
        except Exception as e:  # noqa: BLE001
            logger.warning("es expand_spu: %s", e)
            return []

    def search_outfits_by_skus_batch(
        self,
        sku_ids: list[str],
        size: int = 200,
        *,
        sources: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Batch lookup outfits containing ANY of the given SKU IDs (single terms query)."""
        if not self._client or not sku_ids:
            return []
        ids = [str(x).strip() for x in sku_ids if str(x).strip()]
        if not ids:
            return []
        idx = self._indices["outfits"]
        filters: list[dict[str, Any]] = [{"terms": {"sku_ids": ids}}]
        source_terms = [str(x).strip() for x in (sources or []) if str(x).strip()]
        if source_terms:
            filters.append({"terms": {"source": source_terms}})
        body = {
            "size": max(1, int(size)),
            "query": {"bool": {"filter": filters}},
        }
        try:
            res = self._client.search(index=idx, body=body)
            hits = res.get("hits", {}).get("hits", [])
            return [
                h.get("_source") or {}
                for h in hits
                if isinstance(h.get("_source"), dict)
            ]
        except Exception as e:  # noqa: BLE001
            logger.warning("es search_outfits_by_skus_batch: %s", e)
            return []

    def outfit_source_counts(self) -> list[dict[str, Any]]:
        if not self._client:
            return []
        idx = self._indices["outfits"]
        body = {
            "size": 0,
            "query": {
                "bool": {
                    "filter": [
                        {"terms": {"source": list(OPERATIONAL_OUTFIT_SOURCES)}},
                    ],
                },
            },
            "aggs": {
                "by_source": {
                    "terms": {"field": "source", "size": 100},
                },
            },
        }
        try:
            res = self._client.search(index=idx, body=body)
            buckets = (
                res.get("aggregations", {})
                .get("by_source", {})
                .get("buckets", [])
            )
            rows: list[dict[str, Any]] = []
            for bucket in buckets:
                key = str(bucket.get("key") or "").strip()
                if not key:
                    continue
                rows.append({
                    "source": key,
                    "count": int(bucket.get("doc_count") or 0),
                })
            return rows
        except Exception as e:  # noqa: BLE001
            logger.warning("es outfit_source_counts: %s", e)
            return []

    def outfit_color_series_counts(
        self,
        *,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        if not self._client:
            return []
        idx = self._indices["outfits"]
        filters: list[dict[str, Any]] = []
        if source:
            filters.append({"term": {"source": source}})
        else:
            # 默认只统计运营来源，排除合成款(synth_*)污染色系 facet 计数
            filters.append(
                {"terms": {"source": list(OPERATIONAL_OUTFIT_SOURCES)}},
            )
        body: dict[str, Any] = {
            "size": 0,
            "aggs": {
                "by_color_series": {
                    "terms": {"field": "color_series_tags", "size": 50},
                },
            },
        }
        if filters:
            body["query"] = {"bool": {"filter": filters}}
        else:
            body["query"] = {"match_all": {}}
        try:
            res = self._client.search(index=idx, body=body)
            buckets = (
                res.get("aggregations", {})
                .get("by_color_series", {})
                .get("buckets", [])
            )
            rows: list[dict[str, Any]] = []
            for bucket in buckets:
                key = str(bucket.get("key") or "").strip()
                if not key:
                    continue
                rows.append({
                    "color_series": key,
                    "count": int(bucket.get("doc_count") or 0),
                })
            return rows
        except Exception as e:  # noqa: BLE001
            logger.warning("es outfit_color_series_counts: %s", e)
            return []

    def outfit_season_counts(
        self,
        *,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        if not self._client:
            return []
        idx = self._indices["outfits"]
        filters: list[dict[str, Any]] = []
        if source:
            filters.append({"term": {"source": source}})
        else:
            # 默认只统计运营来源，排除合成款(synth_*)污染季节 facet 计数
            filters.append(
                {"terms": {"source": list(OPERATIONAL_OUTFIT_SOURCES)}},
            )
        body: dict[str, Any] = {
            "size": 0,
            "aggs": {
                "by_season": {
                    "terms": {"field": "season", "size": 20},
                },
            },
        }
        if filters:
            body["query"] = {"bool": {"filter": filters}}
        else:
            body["query"] = {"match_all": {}}
        try:
            res = self._client.search(index=idx, body=body)
            buckets = (
                res.get("aggregations", {})
                .get("by_season", {})
                .get("buckets", [])
            )
            rows: list[dict[str, Any]] = []
            for bucket in buckets:
                key = str(bucket.get("key") or "").strip()
                if not key:
                    continue
                rows.append({
                    "season": key,
                    "count": int(bucket.get("doc_count") or 0),
                })
            return rows
        except Exception as e:  # noqa: BLE001
            logger.warning("es outfit_season_counts: %s", e)
            return []

    def browse_outfits(
        self,
        offset: int,
        size: int,
        *,
        source: str | None = None,
        color_series: str | None = None,
        season: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        if not self._client:
            return [], 0
        idx = self._indices["outfits"]
        filters: list[dict[str, Any]] = []
        if source:
            filters.append({"term": {"source": source}})
        else:
            # 默认只展示运营来源，排除合成款(synth_*)混入 browse 列表
            filters.append(
                {"terms": {"source": list(OPERATIONAL_OUTFIT_SOURCES)}},
            )
        if color_series:
            filters.append({"term": {"color_series_tags": color_series}})
        if season:
            filters.append({"term": {"season": season}})
        if filters:
            query: dict[str, Any] = {"bool": {"filter": filters}}
        else:
            query = {"match_all": {}}
        body = {
            "from": max(0, int(offset)),
            "size": max(1, int(size)),
            "query": query,
        }
        try:
            res = self._client.search(index=idx, body=body)
            hits_obj = res.get("hits", {})
            total_obj = hits_obj.get("total", 0)
            total = (
                int(total_obj.get("value") or 0)
                if isinstance(total_obj, dict)
                else int(total_obj or 0)
            )
            hits = hits_obj.get("hits", [])
            rows = [
                h.get("_source") or {}
                for h in hits
                if isinstance(h.get("_source"), dict)
            ]
            return rows, total
        except Exception as e:  # noqa: BLE001
            logger.warning("es browse_outfits: %s", e)
            return [], 0

    # 儿童年龄段全集（用于「成人」= 不属于任何儿童年龄段 的过滤语义）
    _KID_AGES = ("小童", "中大童", "婴幼童", "通码")

    def browse_skus(
        self,
        offset: int,
        size: int,
        *,
        gender: list[str] | None = None,
        age: list[str] | None = None,
        season: list[str] | None = None,
        color_series: list[str] | None = None,
        category_l2: list[str] | None = None,
        series: list[str] | None = None,
        role: list[str] | None = None,
        up_time_since: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """按结构化筛选条件分页浏览 SKU（镜像 ``browse_outfits``）。

        各维度间 AND、维度内多值 OR（terms）。gender/season/color_series 为 list
        字段；age 含特殊值「成人」= 不属于任何儿童年龄段；series/category_l2/role
        为标量 keyword。按 up_time 倒序（新品优先）。up_time_since（yyyy-MM-dd）
        非空时叠加 up_time >= since 范围过滤，仅检索该日期之后上架的 SKU。
        """
        if not self._client:
            return [], 0
        idx = self._indices["skus"]
        filters: list[dict[str, Any]] = []

        def _clean(vals: list[str] | None) -> list[str]:
            return [str(v).strip() for v in (vals or []) if str(v).strip()]

        g = _clean(gender)
        if g:
            # 儿童 是汇总值 → 展开为 [儿童, 男童, 女童]
            expanded: list[str] = []
            for v in g:
                if v == "儿童":
                    expanded.extend(["儿童", "男童", "女童"])
                else:
                    expanded.append(v)
            filters.append({"terms": {"gender": sorted(set(expanded))}})

        a = _clean(age)
        if a:
            kid = [v for v in a if v in self._KID_AGES]
            want_adult = "成人" in a
            age_clauses: list[dict[str, Any]] = []
            if kid:
                age_clauses.append({"terms": {"age": kid}})
            if want_adult:
                # 成人 = age 不属于任何儿童年龄段（含空值/None）
                age_clauses.append(
                    {"bool": {"must_not": [{"terms": {"age": list(self._KID_AGES)}}]}}
                )
            if len(age_clauses) == 1:
                filters.append(age_clauses[0])
            elif age_clauses:
                filters.append({"bool": {"should": age_clauses, "minimum_should_match": 1}})

        s = _clean(season)
        if s:
            filters.append({"terms": {"season": s}})  # season 为 list 字段

        cs = _clean(color_series)
        if cs:
            filters.append({"terms": {"color_series": cs}})  # list 字段

        c2 = _clean(category_l2)
        if c2:
            filters.append({"terms": {"category_l2": c2}})

        se = _clean(series)
        if se:
            filters.append({"terms": {"series": se}})

        r = _clean(role)
        if r:
            filters.append({"terms": {"role": r}})

        since = (up_time_since or "").strip()
        if since:
            # up_time 为 date 字段（yyyy-MM-dd HH:mm:ss||yyyy-MM-dd），
            # range gte 字符串口径与 build_up_time_es_filter 一致。
            filters.append({"range": {"up_time": {"gte": since}}})

        query: dict[str, Any] = {"bool": {"filter": filters}} if filters else {"match_all": {}}
        body = {
            "from": max(0, int(offset)),
            "size": max(1, int(size)),
            "query": query,
            "track_total_hits": True,
            "sort": [{"up_time": {"order": "desc"}}],
        }
        try:
            res = self._client.search(index=idx, body=body)
            hits_obj = res.get("hits", {})
            total_obj = hits_obj.get("total", 0)
            total = (
                int(total_obj.get("value") or 0)
                if isinstance(total_obj, dict)
                else int(total_obj or 0)
            )
            hits = hits_obj.get("hits", [])
            rows = [
                h.get("_source") or {}
                for h in hits
                if isinstance(h.get("_source"), dict)
            ]
            return rows, total
        except Exception as e:  # noqa: BLE001
            logger.warning("es browse_skus: %s", e)
            return [], 0

    def sku_facets(self) -> dict[str, list[dict[str, Any]]]:
        """返回 SKU 索引上数据驱动的分面（类目/系列），按 doc_count 倒序。"""
        if not self._client:
            return {}
        idx = self._indices["skus"]
        body = {
            "size": 0,
            "aggs": {
                "category_l2": {"terms": {"field": "category_l2", "size": 300}},
                "series": {"terms": {"field": "series", "size": 300}},
            },
        }
        out: dict[str, list[dict[str, Any]]] = {"category_l2": [], "series": []}
        try:
            res = self._client.search(index=idx, body=body)
            aggs = res.get("aggregations", {}) or {}
            for key in ("category_l2", "series"):
                buckets = aggs.get(key, {}).get("buckets", []) or []
                rows: list[dict[str, Any]] = []
                for b in buckets:
                    val = str(b.get("key") or "").strip()
                    if not val:
                        continue
                    rows.append({"value": val, "count": int(b.get("doc_count") or 0)})
                out[key] = rows
            return out
        except Exception as e:  # noqa: BLE001
            logger.warning("es sku_facets: %s", e)
            return out


    def search_outfits(
        self,
        query: str,
        size: int,
    ) -> list[dict[str, Any]]:
        if not self._client:
            return []
        idx = self._indices["outfits"]
        body = {
            "size": max(1, int(size)),
            "query": {
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": query,
                                "fields": ["name^2", "search_text", "items.title"],
                            },
                        },
                    ],
                    # 排除合成款(synth_*)混入文本搜索结果
                    "filter": [
                        {"terms": {"source": list(OPERATIONAL_OUTFIT_SOURCES)}},
                    ],
                },
            },
        }
        try:
            res = self._client.search(index=idx, body=body)
            hits = res.get("hits", {}).get("hits", [])
            return [
                h.get("_source") or {}
                for h in hits
                if isinstance(h.get("_source"), dict)
            ]
        except Exception as e:  # noqa: BLE001
            logger.warning("es search_outfits: %s", e)
            return []

    def index_doc(
        self,
        index_key: str,
        doc: dict[str, Any],
        doc_id: str | None = None,
        refresh: bool = True,
    ) -> str | None:
        """单文档 index,返回 ES `_id`;不可用/失败/未知索引返回 None。"""
        if not self._client or not isinstance(doc, dict):
            return None
        if index_key not in self._indices:
            return None
        idx = self._indices[index_key]
        try:
            if doc_id:
                res = self._client.index(
                    index=idx, body=doc, id=doc_id, refresh=refresh,
                )
            else:
                res = self._client.index(index=idx, body=doc, refresh=refresh)
            return str(res.get("_id") or "")
        except Exception as e:  # noqa: BLE001
            logger.warning("es index_doc %s: %s", index_key, e)
            return None

    def delete_doc(self, index_key: str, doc_id: str) -> bool:
        """按 `_id` 删单文档;命中 True,未命中/不可用 False。"""
        if not self._client or not doc_id:
            return False
        idx = self._indices[index_key]
        try:
            res = self._client.delete(index=idx, id=doc_id, refresh=True)
            return str(res.get("result") or "") in ("deleted", "ok")
        except Exception as e:  # noqa: BLE001
            logger.warning("es delete_doc %s/%s: %s", index_key, doc_id, e)
            return False

    def search_docs(
        self,
        index_key: str,
        body: dict[str, Any],
    ) -> list[tuple[str, dict[str, Any]]]:
        """发出 search,返回 [(doc_id, _source), ...];不可用/未知索引返回 []。"""
        if not self._client:
            return []
        if index_key not in self._indices:
            return []
        idx = self._indices[index_key]
        try:
            res = self._client.search(index=idx, body=body)
            hits = res.get("hits", {}).get("hits", [])
            out: list[tuple[str, dict[str, Any]]] = []
            for h in hits:
                src = h.get("_source")
                if isinstance(src, dict):
                    out.append((str(h.get("_id") or ""), src))
            return out
        except Exception as e:  # noqa: BLE001
            logger.warning("es search_docs %s: %s", index_key, e)
            return []
