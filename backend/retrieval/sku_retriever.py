"""SKU 召回：Milvus + 本地文本。"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from backend.api_debug import log_text_vector_recall_io
from backend.embedding_client import embed_text
from backend.local_data_store import LocalDataStore
from backend.retrieval.data_facade import DataFacade
from backend.retrieval.es_client import EsClient
from backend.intent.category_l2_pairing import (
    build_category_l2_milvus_expr,
    build_group_brand_milvus_expr,
    merge_milvus_expr,
)
from backend.models import normalize_season
from backend.retrieval.milvus_client import MilvusClient
from backend.retrieval.hybrid_search import FilaSkuHybridSearcher
from backend.retrieval.up_time_filter import build_up_time_milvus_expr

logger = logging.getLogger(__name__)
from backend.config import get_elasticsearch_indices, load_config


class SkuRetriever:
    def __init__(
        self,
        store: LocalDataStore,
        es: Optional[EsClient] = None,
        milvus: Optional[MilvusClient] = None,
        data: DataFacade | None = None,
        hybrid_searcher: Optional[FilaSkuHybridSearcher] = None,
    ) -> None:
        self._store = store
        self._es = es or EsClient()
        self._milvus = milvus or MilvusClient()
        self._data = data or DataFacade(store, self._es)
        self._hybrid = hybrid_searcher or FilaSkuHybridSearcher()

    @staticmethod
    def _build_season_milvus_expr(seasons: list[str] | None) -> str | None:
        """构造 Milvus season 过滤 expr。

        season 在 Milvus 中存储为逗号分隔字符串（如 "春,夏"），
        使用 like 匹配任一目标季节。无季节要求时返回 None。
        """
        if not seasons:
            return None
        normalized = normalize_season(seasons)
        if not normalized:
            return None
        parts = [f'season like "%{s}%"' for s in normalized]
        if len(parts) == 1:
            return f"({parts[0]} or season == \"\")"
        return f"({' or '.join(parts)} or season == \"\")"

    def recall_by_vector(
        self,
        vector: Optional[List[float]],
        top_k: Optional[int] = None,
        *,
        category_l2_filter: list[str] | None = None,
        season_filter: list[str] | None = None,
        attr_expr: str | None = None,
        group_brand: str | None = None,
        skip_up_time: bool = False,
    ) -> List[Tuple[str, float, float]]:
        """SKU 图向量召回。

        返回 (sku_id, similarity, milvus_raw) 列表。
        COSINE/IP 下 milvus_raw 即 Milvus 返回的 score，越大越相似；
        similarity 与 milvus_raw 经 hit_to_similarity 对齐（COSINE 时相同）。

        ``attr_expr``：结构化属性过滤片段（如 ``is_intimate == "false"``、
        ``length_class != "short"``），由调用方按锚点上下文用
        ``build_attr_milvus_expr`` 构造后传入，直接并入 Milvus expr 粗排。

        ``group_brand``：集团品牌过滤（如 ``斐乐大货``），非空时并入 expr；
        为空则不限制（现有查询零行为变化）。

        ``skip_up_time``：意图解析模块图向量近邻（图锚点/候选、SKU 图片
        fallback）下钻时跳过 ``up_time >= up_time_since`` 片段——输入 sku_id
        检索不应被上架时间下限过滤，仅搭配召回阶段过滤。
        """
        cfg = load_config()
        rec = cfg.get("recommend") or {}
        k = top_k or int(rec.get("milvus_top_k") or 20)
        min_sim = float(rec.get("sku_vector_min_similarity") or 0.0)
        if not vector:
            return []
        cat2_expr = build_category_l2_milvus_expr(category_l2_filter or [])
        season_expr = self._build_season_milvus_expr(season_filter)
        gb_expr = build_group_brand_milvus_expr(group_brand)
        # 全局上架时间下限（build_up_time_milvus_expr 读取 recommend 配置）：
        # skip_up_time 时跳过；未配置/禁用时 None 由 merge 跳过
        expr = merge_milvus_expr(
            cat2_expr, season_expr, attr_expr, gb_expr,
            build_up_time_milvus_expr() if not skip_up_time else None,
        )
        pairs = self._milvus.search_sku_vectors(vector, k, expr=expr)

        def row(sid: str, raw_hit: float) -> tuple[str, float, float]:
            r = float(raw_hit)
            sim = self._milvus.hit_to_similarity(r)
            return (sid, sim, r)

        if min_sim <= 0.0:
            results = [row(sid, dist) for sid, dist in pairs]
        else:
            results = []
            for sid, dist in pairs:
                sid2, sim, d = row(sid, dist)
                if sim >= min_sim:
                    results.append((sid2, sim, d))

        logger.info(
            "milvus_recall: min_sim=%.4f, top_k=%d, raw_hits=%d, after_filter=%d",
            min_sim, k, len(pairs), len(results),
        )
        for sid, sim, raw in results:
            logger.info("  sku=%s sim=%.4f raw=%.4f", sid, sim, raw)

        return results

    def recall_by_text_vector_keywords(
        self,
        keywords: list[str],
        top_k_per_keyword: Optional[int] = None,
        *,
        role_filter: str | None = None,
        gender_filter: str | None = None,
        age_filter: str | None = None,
        category_l2_filter: list[str] | None = None,
        color_series_filter: list[str] | None = None,
        trace_id: str | None = None,
        fallback_on_empty: bool = True,
        attr_expr: str | None = None,
        min_similarity_override: float | None = None,
        color_series_match_mode: str = "auto",
        group_brand: str | None = None,
        skip_up_time: bool = False,
    ) -> List[Tuple[str, float, float]]:
        """多关键词文本向量近邻：每个关键词 embed 后检索，按 SKU 取最大相似度。

        与 ``recall_by_vector``（主图多模态 product_vector）独立。
        ``category_l2_filter`` / ``color_series_filter`` 始终写入 Milvus expr。
        ``role_filter`` / ``gender_filter`` / ``age_filter`` 仅当 ``milvus.text_vector_expr_filter``
        为 true 时写入 expr（fila 集合无这几字段，配置为 false 时由调用方 post-filter）。
        ``attr_expr``：结构化属性过滤片段（is_intimate/length_class/layer/coverage），
        同样受 ``text_vector_expr_filter`` 开关控制；未传入时默认并入
        ``is_intimate == "false"``（贴身内衣不进文本向量候选）。
        ``color_series_match_mode``：strict（仅纯色 SKU）、relaxed（纯色+多色）、
        auto（先 strict，不足 top_k 则 relaxed 补足，纯色排前）。默认 auto。
        ``skip_up_time``：progressive relax 链尾放宽 up_time 时跳过该 expr 片段。
        ``fallback_on_empty``：保留以兼容旧调用方，现已无行为（0 命中放宽改由
        ``run_with_progressive_relax`` 驱动器在调用方逐个丢弃 slot 实现）。
        """
        if not keywords:
            return []
        cfg = load_config()
        rec = cfg.get("recommend") or {}
        mv = cfg.get("milvus") or {}
        k = top_k_per_keyword or int(rec.get("text_milvus_top_k") or 5)
        if min_similarity_override is not None:
            min_sim = float(min_similarity_override)
        else:
            min_sim = float(rec.get("sku_text_vector_min_similarity") or 0.0)
        collection = str(
            (mv.get("collections") or {}).get("sku_text_vectors")
            or "fila_sku_text_vectors",
        )
        vector_field = str(mv.get("text_vector_field") or "text_vector")
        # fila_sku_text_vectors 含 role/gender 字段，可通过 milvus.text_vector_expr_filter 控制是否过滤
        scalar_expr = bool(mv.get("text_vector_expr_filter", True))

        def _build_color_series_expr(
            cs: list[str] | None,
            mode: str,
        ) -> str | None:
            """color_series (ARRAY) 匹配 expr。

            relaxed: array_contains_any(color_series, [...])
            strict:  array_length(color_series) == 1 && array_contains_any(...)
            """
            if not cs:
                return None
            vals = [s.strip() for s in cs if s.strip()]
            if not vals:
                return None
            if "多色系" not in vals:
                vals.append("多色系")
            in_list = ", ".join(f'"{s}"' for s in vals)
            contains = f"array_contains_any(color_series, [{in_list}])"
            if mode == "strict":
                return f"array_length(color_series) == 1 && {contains}"
            return contains

        def _build_gender_expr(g: str) -> str:
            if g == "儿童":
                return 'array_contains_any(gender, ["儿童", "男童", "女童"])'
            return f'array_contains(gender, "{g}")'

        def _build_age_expr(a: str) -> str:
            # 指定童装段时同时命中通码（同款覆盖全段），空值不命中以排除成人款
            if a == "通码":
                return 'age == "通码"'
            return f'age in ["{a}", "通码"]'

        def _build_expr(cs_mode: str) -> str:
            return merge_milvus_expr(
                f'role == "{role_filter}"' if role_filter and scalar_expr else None,
                _build_gender_expr(gender_filter) if gender_filter and scalar_expr else None,
                _build_age_expr(age_filter) if age_filter and scalar_expr else None,
                build_category_l2_milvus_expr(category_l2_filter or []),
                _build_color_series_expr(color_series_filter, cs_mode),
                # 结构化属性过滤；未传时默认排除贴身内衣（受 text_vector_expr_filter 开关控制）
                (attr_expr or 'is_intimate == "false"') if scalar_expr else None,
                # 集团品牌过滤（可选，非空时并入 expr）
                build_group_brand_milvus_expr(group_brand),
                # 全局上架时间下限：up_time >= config.recommend.up_time_since
                #（progressive relax 链尾 skip_up_time 时跳过；禁用时 None 由 merge 跳过）
                build_up_time_milvus_expr() if not skip_up_time else None,
            )

        cs_mode = color_series_match_mode if color_series_match_mode in ("strict", "relaxed") else "auto"
        primary_mode = "strict" if cs_mode == "auto" else cs_mode
        expr = _build_expr(primary_mode)

        def _search_with_expr(search_expr: str) -> tuple[list[tuple[str, float, float]], list[dict]]:
            merged: dict[str, tuple[float, float]] = {}
            per_keyword_log: list[dict] = []

            def _embed_and_search_kw(kw: str) -> tuple[str, list[float] | None, list[dict[str, float | str]]]:
                vec = embed_text(kw)
                kw_hits: list[dict[str, float | str]] = []
                if vec:
                    # 关键词原文喂 BM25 sparse + 关键词 embedding 喂 dense（hybrid）；
                    # 集合未重建（无 sparse_vector）时自动降级 dense-only。
                    for sid, dist in self._milvus.search_sku_text_vectors(vec, k, expr=search_expr, query_text=kw):
                        sim = self._milvus.hit_to_similarity(float(dist))
                        kw_hits.append({"sku_id": sid, "similarity": round(sim, 4), "milvus_raw": round(float(dist), 4)})
                return kw, vec, kw_hits

            workers = min(len(keywords), 4)
            if workers > 1:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {pool.submit(_embed_and_search_kw, kw): kw for kw in keywords}
                    for fut in as_completed(futures):
                        kw, vec, kw_hits = fut.result()
                        for hit in kw_hits:
                            sid = str(hit["sku_id"])
                            sim = float(hit["similarity"])
                            raw = float(hit["milvus_raw"])
                            prev = merged.get(sid)
                            if prev is None or sim > prev[0]:
                                merged[sid] = (sim, raw)
                        per_keyword_log.append({
                            "keyword": kw, "embed_ok": vec is not None,
                            "vector_dim": len(vec) if vec else 0,
                            "milvus_input": {"collection": collection, "vector_field": vector_field, "top_k": k, "expr": search_expr},
                            "milvus_output": kw_hits, "hit_count": len(kw_hits),
                        })
            else:
                for kw in keywords:
                    kw, vec, kw_hits = _embed_and_search_kw(kw)
                    for hit in kw_hits:
                        sid = str(hit["sku_id"])
                        sim = float(hit["similarity"])
                        raw = float(hit["milvus_raw"])
                        prev = merged.get(sid)
                        if prev is None or sim > prev[0]:
                            merged[sid] = (sim, raw)
                    per_keyword_log.append({
                        "keyword": kw, "embed_ok": vec is not None,
                        "vector_dim": len(vec) if vec else 0,
                        "milvus_input": {"collection": collection, "vector_field": vector_field, "top_k": k, "expr": search_expr},
                        "milvus_output": kw_hits, "hit_count": len(kw_hits),
                    })

            rows = [(sid, sim, raw) for sid, (sim, raw) in merged.items()]
            rows.sort(key=lambda x: x[1], reverse=True)
            if min_sim > 0.0:
                rows = [r for r in rows if r[1] >= min_sim]
            return rows, per_keyword_log

        rows, per_keyword_log = _search_with_expr(expr)

        # auto 模式：strict 不足 k 则 relaxed 补足，纯色排前
        if cs_mode == "auto" and color_series_filter and len(rows) < k:
            relaxed_expr = _build_expr("relaxed")
            relaxed_rows, relaxed_log = _search_with_expr(relaxed_expr)
            strict_sids = {sid for sid, _, _ in rows}
            for sid, sim, raw in relaxed_rows:
                if sid not in strict_sids:
                    rows.append((sid, sim, raw))
            rows.sort(key=lambda x: x[1], reverse=True)
            per_keyword_log.extend(relaxed_log)

        merged_log = [
            {"sku_id": sid, "similarity": round(sim, 4), "milvus_raw": round(raw, 4)}
            for sid, sim, raw in rows
        ]
        log_text_vector_recall_io(
            trace_id=trace_id,
            collection=collection,
            vector_field=vector_field,
            top_k_per_keyword=k,
            min_similarity=min_sim,
            per_keyword=per_keyword_log,
            merged_output=merged_log,
        )

        # 0 命中放宽已上移到调用方 run_with_progressive_relax 驱动器
        #（逐个丢弃 slot 并重建 attr_expr），此处不再递归 fallback_on_empty。
        return rows

    def recall_by_hybrid(
        self,
        keywords: list[str],
        top_k_per_keyword: Optional[int] = None,
        *,
        role_filter: str | None = None,
        gender_filter: str | None = None,
        age_filter: str | None = None,
        category_l2_filter: list[str] | None = None,
        color_series_filter: list[str] | None = None,
        trace_id: str | None = None,
        fallback_on_empty: bool = True,
        attr_expr: str | None = None,
        color_series_match_mode: str = "auto",
        group_brand: str | None = None,
        skip_up_time: bool = False,
    ) -> List[Tuple[str, float, float]]:
        """hybrid(BM25+dense)文本召回：每关键词一次 search_hybrid，按 sku 取 max score。

        hybrid score 非 COSINE 量纲，默认 min_sim=0（不过滤），阈值留待 eval 调参。
        ``skip_up_time``：progressive relax 链尾放宽 up_time 时跳过该 expr 片段。
        ``fallback_on_empty``：保留以兼容旧调用方，现已无行为（0 命中放宽改由
        ``run_with_progressive_relax`` 驱动器在调用方逐个丢弃 slot 实现；
        hybrid→dense 的旧 fallback 由调用方 search_fn 的双 leg 结构承担）。
        """
        if not keywords:
            return []
        cfg = load_config()
        rec = cfg.get("recommend") or {}
        k = top_k_per_keyword or int(rec.get("text_milvus_top_k") or 5)
        cs_mode = color_series_match_mode if color_series_match_mode in ("strict", "relaxed") else "auto"
        primary_mode = "strict" if cs_mode == "auto" else cs_mode

        def _build_color_expr(cs: list[str] | None, mode: str) -> str | None:
            if not cs:
                return None
            vals = [s.strip() for s in cs if s.strip()]
            if not vals:
                return None
            if "多色系" not in vals:
                vals.append("多色系")
            in_list = ", ".join(f'"{s}"' for s in vals)
            contains = f"array_contains_any(color_series, [{in_list}])"
            if mode == "strict":
                return f"array_length(color_series) == 1 && {contains}"
            return contains

        def _build_expr(cs_mode_inner: str) -> str | None:
            age_expr: str | None = None
            if age_filter == "通码":
                age_expr = 'age == "通码"'
            elif age_filter:
                age_expr = f'age in ["{age_filter}", "通码"]'
            return merge_milvus_expr(
                f'role == "{role_filter}"' if role_filter else None,
                f'array_contains_any(gender, ["{gender_filter}"])' if gender_filter else None,
                age_expr,
                build_category_l2_milvus_expr(category_l2_filter or []),
                _build_color_expr(color_series_filter, cs_mode_inner),
                attr_expr or 'is_intimate == "false"',
                build_group_brand_milvus_expr(group_brand),
                build_up_time_milvus_expr() if not skip_up_time else None,
            )

        def _run(search_expr: str | None) -> list[tuple[str, float, float]]:
            merged: dict[str, tuple[float, float]] = {}
            for kw in keywords:
                hits = self._hybrid.search_hybrid(
                    kw, expr=search_expr, limit=k, skip_rewrite=True,
                    output_fields=["sku_id"],
                )
                for h in hits:
                    sid = str(h.get("sku_id") or "")
                    if not sid:
                        continue
                    raw = float(h.get("score") or 0)
                    sim = self._milvus.hit_to_similarity(raw)
                    prev = merged.get(sid)
                    if prev is None or sim > prev[0]:
                        merged[sid] = (sim, raw)
            rows = [(sid, sim, raw) for sid, (sim, raw) in merged.items()]
            rows.sort(key=lambda x: x[1], reverse=True)
            return rows

        rows = _run(_build_expr(primary_mode))
        if cs_mode == "auto" and color_series_filter and len(rows) < k:
            relaxed = _run(_build_expr("relaxed"))
            have = {sid for sid, _, _ in rows}
            for sid, sim, raw in relaxed:
                if sid not in have:
                    rows.append((sid, sim, raw))
            rows.sort(key=lambda x: x[1], reverse=True)

        # 0 命中放宽已上移到调用方 run_with_progressive_relax 驱动器
        #（逐个丢弃 slot 并重建 attr_expr；hybrid→dense 的旧 fallback 由调用方
        # search_fn 的双 leg 结构承担），此处不再内部 fallback。
        return rows

    def recall_by_text(
        self,
        q: str,
        limit: int = 30,
        *,
        trace_id: str | None = None,
    ) -> List[str]:
        from backend.api_debug import log_text_search_recall_io

        cfg = load_config()
        es_idx = get_elasticsearch_indices(cfg)
        if self._es.available:
            ids = self._es.search_skus(q, None, limit)
            if ids:
                log_text_search_recall_io(
                    trace_id=trace_id,
                    entity="sku",
                    channel="elasticsearch",
                    query=q,
                    limit=limit,
                    output_ids=ids,
                    extra={"index": es_idx["skus"]},
                )
                return ids
        # 本地 JSONL 加载已停用（数据走 ES）；ES 不可用或无命中时返回空。
        log_text_search_recall_io(
            trace_id=trace_id,
            entity="sku",
            channel="elasticsearch" if self._es.available else "unavailable",
            query=q,
            limit=limit,
            output_ids=[],
            extra={"index": es_idx["skus"]},
        )
        return []

    def get_sku(self, sku_id: str) -> Optional[Dict[str, Any]]:
        return self._data.get_sku(sku_id)

    def expand_spu(self, spu_id: str) -> list[str]:
        return self._data.expand_spu(spu_id)
