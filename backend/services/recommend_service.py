"""推荐编排：召回 + 排序 + 溯源 + 耗时埋点。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from time import perf_counter
from typing import Any, AsyncIterator

from backend.api_debug import (
    debug_api_io_enabled,
    log_flow,
    log_recommend_stage,
)
from backend.recall_pathway import (
    RECALL_PATHWAY_LABELS,
    RecallPathway,
    chat_recall_pathway_bundle,
    pathway_log_fields,
)
from backend.embedding_client import embed_image_url
from backend.image_understanding import image_query_embedding
from backend.image_saver import save_image_async
from backend.jsonl_logger import JsonlLogger
from backend.services.request_audit import (
    RequestAuditLogger,
    build_input_block,
    build_recommend_doc,
    build_regenerate_doc,
    now_iso,
)
from backend.local_data_store import LocalDataStore
from backend.llm_client import (
    generate_outfit_reason_payload,
    outfit_reason_key,
    _reason_mode,
    _reason_one_outfit,
)
from backend.ranking.vision_reasoner import generate_outfit_reason_payload_vision
from backend.models import (
    ChatRequest,
    ExternalRecommendRequest,
    ExternalRegenerateReasonRequest,
    RecommendOutfitsRequest,
    RecommendSkusRequest,
    RegenerateReasonRequest,
    UserIntent,
)
from backend.query_understanding import backfill_intent_from_sku, find_sku_token, parse_user_intent
from backend.intent.intent_engine import IntentResult
from backend.ranking.item_ranker import rank_skus
from backend.ranking.scoring import age_conflict, gender_conflict, season_conflict
from backend.retrieval.data_facade import DataFacade
from backend.retrieval.sku_retriever import SkuRetriever
from backend.services.card_builder import outfit_card, sku_card
from backend.services.external_recommend import (
    fetch_image_url_to_base64,
    reshape_outfits_to_external,
)
from backend.services.outfit_recall import (
    coarse_rank_outfits,
    merge_and_dedupe_outfits,
    merge_and_rank_outfits,
    multi_path_recall,
    rank_deduped_outfits,
)
from backend.services.synthetic_outfit import synth_card_to_doc
from backend.services.tryon_service import batch_tryon_outfits
from backend.config import load_config, get_elasticsearch_hosts, get_elasticsearch_indices, rank_outfit_limit
from eval.defect_analyzer import OutfitDefectAnalyzer, summarize_defects

logger = logging.getLogger(__name__)


def _item_sku_id(item: dict[str, Any]) -> str:
    """从 item 中提取 SKU ID（兼容 sku_id / skuId / attrAlias / idAlias）。"""
    raw = (
        item.get("sku_id")
        or item.get("skuId")
        or item.get("attrAlias")
        or item.get("idAlias")
    )
    return str(raw).strip() if raw is not None else ""


def _replace_anchor_item_with_upload(
    outfits: list[dict[str, Any]],
    anchor_sku_id: str,
    upload_anchor: dict[str, Any],
) -> None:
    """将图向量召回搭配中 anchor item 的商品信息替换为上传图片的商品信息。"""
    for outfit in outfits:
        for item in outfit.get("items") or []:
            if _item_sku_id(item) == anchor_sku_id:
                for key in ("sku_id", "spu_id", "title", "price",
                            "display_image", "tryon_image"):
                    if key in upload_anchor:
                        item[key] = upload_anchor[key]


def build_upload_anchor_row(
    *,
    anchor_attrs: dict[str, Any] | None,
    image_anchor_row: dict[str, Any] | None,
    sku_anchor_sim: float,
    image_base64: str | None,
    trace_id: str,
) -> dict[str, Any] | None:
    """构建上传图片的锚点行：identity + 融合属性 + 上传图。

    结构化属性（role/category_l2/length_class/coverage/layer/scene_domain/is_intimate）
    统一取自意图模块融合产出的 ``anchor_attrs``，不再在此处 ad-hoc 派生——
    虚拟图锚点（无高 sim 匹配 SKU）也因此带齐 ``category_l2``，``length_class``
    不再退化为 ``"n/a"``，下游 length_class 预过滤（长袖→排除短裤/五分裤）得以生效。

    identity 分支（与原 if/elif/else 语义一致）：
      - 精确匹配（sim>=1.0）：保留真实 SKU identity，仅替换展示图。
      - 模糊匹配（有 image_anchor_row）：沿用真实 SKU 数据，sku_id 标记为图锚点。
      - 虚拟（无 image_anchor_row）：无匹配 SKU，属性全来自 ``anchor_attrs``。
    """
    if not image_base64:
        return None
    user_tryon_img = f"data:image/jpeg;base64,{image_base64}"
    attrs = anchor_attrs or {}

    if image_anchor_row and sku_anchor_sim >= 1.0:
        return {
            **image_anchor_row,
            **attrs,
            "display_image": user_tryon_img,
            "tryon_image": user_tryon_img,
        }
    if image_anchor_row:
        return {
            **image_anchor_row,
            **attrs,
            "sku_id": f"img_{trace_id[:12]}",
            "spu_id": "",
            "title": "用户上传图片",
            "price": 0.0,
            "display_image": user_tryon_img,
            "tryon_image": user_tryon_img,
        }
    return {
        "sku_id": f"img_{trace_id[:12]}",
        **attrs,
        "title": "用户上传图片",
        "price": 0.0,
        "display_image": user_tryon_img,
        "tryon_image": user_tryon_img,
        "_is_virtual_image_anchor": True,
    }


def _log_chat_sse(
    trace_id: str,
    ev: dict[str, Any],
    *,
    recall_extra: dict[str, Any] | None = None,
) -> None:
    if not debug_api_io_enabled():
        return
    et = str(ev.get("type") or "")
    base: dict[str, Any] = {"trace_id": trace_id, "event": et}
    if recall_extra:
        base.update(recall_extra)
    if et == "session_id":
        log_flow(
            "chat_sse",
            {
                **base,
                "session_id": ev.get("session_id"),
            },
        )
    elif et == "intent":
        log_flow(
            "chat_sse",
            {**base, "intent": ev.get("intent")},
        )
    elif et == "anchor_skus":
        sk = ev.get("skus") or []
        log_flow(
            "chat_sse",
            {
                **base,
                "count": len(sk),
                "sku_ids": [s.get("sku_id") for s in sk],
            },
        )
    elif et == "recall_progress":
        log_flow(
            "chat_sse",
            {
                **base,
                "path": ev.get("path"),
                "status": ev.get("status"),
                "count": ev.get("count"),
            },
        )
    elif et == "recall_done":
        log_flow(
            "chat_sse",
            {
                **base,
                "before_dedupe": ev.get("before_dedupe"),
                "after_dedupe": ev.get("after_dedupe"),
            },
        )
    elif et == "ranking_done":
        log_flow(
            "chat_sse",
            {
                **base,
                "input_count": ev.get("input_count"),
                "output_count": ev.get("output_count"),
                "scoring_method": ev.get("scoring_method"),
                "ranking_elapsed_ms": ev.get("ranking_elapsed_ms"),
            },
        )
    elif et == "outfit_results":
        oo = ev.get("outfits") or []
        log_flow(
            "chat_sse",
            {
                **base,
                "outfit_count": len(oo),
            },
        )
    elif et == "sku_results":
        gg = ev.get("groups") or []
        oo = ev.get("outfits") or []
        log_flow(
            "chat_sse",
            {
                **base,
                "groups": [
                    {"role": g.get("role"), "sku_count": len(g.get("skus") or [])}
                    for g in gg
                ],
                "relation_outfit_count": len(oo),
                "relation_outfit_ids": [x.get("outfit_id") for x in oo[:12]],
            },
        )
    elif et == "text":
        c = str(ev.get("content") or "")
        log_flow(
            "chat_sse",
            {
                **base,
                "content_len": len(c),
                "preview": c[:240],
            },
        )
    elif et == "done":
        log_flow("chat_sse", base)
    else:
        log_flow(
            "chat_sse",
            {**base, "raw_keys": list(ev.keys())},
        )


class RecommendService:
    def __init__(self) -> None:
        self._store = LocalDataStore()
        self._data = DataFacade(self._store)
        self._sku_r = SkuRetriever(self._store, data=self._data)
        self._log = JsonlLogger()
        self._audit = RequestAuditLogger()
        # outfit card 内存缓存：{outfit_id: {card, message, ts}}
        self._outfit_cache: dict[str, dict[str, Any]] = {}

    def _cache_outfit_cards(
        self,
        cards: list[dict[str, Any]],
        message: str = "",
    ) -> None:
        """将推荐产出的 outfit cards 写入内存缓存。"""
        now = perf_counter()
        cfg = (load_config().get("recommend") or {}).get("outfit_cache") or {}
        ttl = float(cfg.get("ttl_seconds") or 3600)
        max_size = int(cfg.get("max_size") or 500)
        # 淘汰过期条目
        expired = [
            k for k, v in self._outfit_cache.items()
            if now - v["ts"] > ttl
        ]
        for k in expired:
            del self._outfit_cache[k]
        # 写入新条目
        for card in cards:
            oid = str(card.get("outfit_id") or "")
            if oid:
                self._outfit_cache[oid] = {
                    "card": card,
                    "message": message,
                    "ts": now,
                }
        # 容量淘汰：按 ts 升序删除最旧的
        if len(self._outfit_cache) > max_size:
            sorted_keys = sorted(
                self._outfit_cache,
                key=lambda k: self._outfit_cache[k]["ts"],
            )
            for k in sorted_keys[: len(self._outfit_cache) - max_size]:
                del self._outfit_cache[k]
        # 持久化合成搭配(synth_*)到 ES outfits 索引：跨 worker / 超 TTL 后
        # regenerate-reason 的 ES 兜底靠它命中。失败仅告警，不阻断推荐主流程。
        self._persist_synth_outfits(cards)

    def _persist_synth_outfits(self, cards: list[dict[str, Any]]) -> None:
        """把合成搭配 card 投影后批量 upsert 进 outfits 索引。

        受 config.recommend.persist_synth_outfits（默认 True）开关控制；ES 不可用
        或投影为空时静默跳过。任一异常仅告警，不影响推荐。
        """
        try:
            rec_cfg = (load_config().get("recommend") or {})
            if not rec_cfg.get("persist_synth_outfits", True):
                return
            seen: dict[str, dict[str, Any]] = {}
            for card in cards or []:
                doc = synth_card_to_doc(card or {})
                if not doc:
                    continue
                seen[str(doc.get("outfit_id") or "")] = doc
            if not seen:
                return
            docs = list(seen.items())
            ok, err = self._data.bulk_upsert_outfits(docs)
            if err:
                logger.warning(
                    "[persist_synth] %d/%d upsert 失败", err, len(docs),
                )
            logger.debug(
                "[persist_synth] upsert %d 合成搭配 (ok=%d, err=%d)",
                len(docs), ok, err,
            )
        except Exception:  # noqa: BLE001
            logger.warning("[persist_synth] 持久化合成搭配失败", exc_info=True)


    def _resolve_anchor_sku(
        self,
        req: RecommendSkusRequest | ChatRequest,
        intent: UserIntent | None = None,
    ) -> str | None:
        if isinstance(req, RecommendSkusRequest):
            if req.anchor_sku_id and self._data.get_sku(req.anchor_sku_id):
                return req.anchor_sku_id
            if req.anchor_sku_id:
                logger.info(
                    "[anchor] anchor_sku_id 不在索引中，按无 SKU 处理: %s",
                    req.anchor_sku_id,
                )
            if req.anchor_spu_id:
                skus = self._sku_r.expand_spu(req.anchor_spu_id)
                return skus[0] if skus else None
            return None
        if req.selected_sku_id and self._data.get_sku(req.selected_sku_id):
            return req.selected_sku_id
        if req.selected_sku_id:
            logger.info(
                "[anchor] selected_sku_id 不在索引中，按无 SKU 处理: %s",
                req.selected_sku_id,
            )
        if req.selected_spu_id:
            skus = self._sku_r.expand_spu(req.selected_spu_id)
            return skus[0] if skus else None
        tok = find_sku_token(req.message or "")
        if tok:
            if self._data.get_sku(tok):
                return tok
        return None

    def _batch_get_sku_map(
        self,
        pairs: list[tuple[str, float, float]],
    ) -> dict[str, dict[str, Any]]:
        """Batch-fetch SKU rows for all Milvus hit IDs in one ES mget call.

        Returns a dict {sku_id: row} for all found SKUs.
        """
        ids = [sid for sid, _sim, _raw in pairs if sid]
        if not ids:
            return {}
        rows = self._data.get_skus(ids)
        return {str(r.get("sku_id") or ""): r for r in rows if r}

    def _groups_rows_from_outfit_companions(
        self,
        anchor: str,
        flt: dict[str, Any],
        target_roles: list[str],
        limit_per_role: int,
    ) -> tuple[list[dict[str, Any]], list[str], int]:
        """从 anchor 所在固定搭配抽取同伴 SKU。"""
        gender = flt.get("gender")
        age = flt.get("age")
        budget = flt.get("budget_max")
        rows, outfit_ids = self._data.companion_skus_by_anchor(
            anchor,
            target_roles,
        )
        raw_n = len(rows)
        by_role: dict[str, list[dict[str, Any]]] = defaultdict(list)
        compat: dict[str, float] = {}
        oq: dict[str, float] = {}
        for row in rows:
            tid_sku = str(row.get("sku_id") or "")
            if not tid_sku:
                continue
            if row.get("role") == "unknown":
                continue
            if gender_conflict(row.get("gender"), gender):
                continue
            if age and age_conflict(row.get("age"), age):
                continue
            tp = float(row.get("price") or 0.0)
            if budget and budget > 0 and tp > float(budget) * 1.05:
                continue
            role = str(row.get("role") or "")
            compat[tid_sku] = 1.0
            oq[tid_sku] = 1.0
            c = dict(row)
            by_role[role].append(c)
        groups_out: list[dict[str, Any]] = []
        for role, rows in by_role.items():
            ranked = rank_skus(
                rows,
                intent_gender=gender,
                intent_season=list(flt.get("season") or []),
                intent_tags=list(flt.get("occasion_tags") or [])
                + list(flt.get("style_tags") or []),
                budget_max=float(budget) if budget else None,
                compat_score=compat,
                outfit_quality=oq,
            )
            top_rows = [r for _, r in ranked[:limit_per_role]]
            if top_rows:
                groups_out.append({"role": role, "rows": top_rows})
        return groups_out, list(dict.fromkeys(outfit_ids)), raw_n

    def recommend_skus(
        self,
        req: RecommendSkusRequest,
        *,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        tid = trace_id or uuid.uuid4().hex
        t0 = perf_counter()
        last = t0
        timings: dict[str, int] = {}

        def lap(stage: str, **kw: Any) -> None:
            nonlocal last
            now = perf_counter()
            em = int((now - last) * 1000)
            sm = int((now - t0) * 1000)
            timings[stage] = em
            last = now
            log_recommend_stage(
                tid,
                stage,
                elapsed_ms=em,
                since_request_ms=sm,
                **kw,
            )

        anchor = self._resolve_anchor_sku(req)
        self._log.log(
            "request_received",
            "recommend_service",
            {"endpoint": "recommend/skus", "anchor": anchor},
            trace_id=tid,
        )
        if not anchor:
            lap("relations_pack", reason="no_anchor")
            total_ms = int((perf_counter() - t0) * 1000)
            self._log.log(
                "request_completed",
                "recommend_service",
                {"endpoint": "recommend/skus", "total_ms": total_ms, "timings": timings},
                trace_id=tid,
            )
            log_recommend_stage(
                tid,
                "recommend_skus_skip",
                elapsed_ms=0,
                since_request_ms=int((perf_counter() - t0) * 1000),
                reason="no_anchor",
                **pathway_log_fields(RecallPathway.SKU_SKIPPED_NO_ANCHOR),
            )
            return {
                "anchor_sku_id": None,
                "groups": [],
                "source_outfit_ids": [],
            }
        flt = req.filters or {}
        gender = flt.get("gender")
        budget = flt.get("budget_max")
        target_roles = req.target_roles or ["bottoms", "shoes"]
        groups_rows, outfit_ids, raw_n = self._groups_rows_from_outfit_companions(
            anchor,
            flt,
            target_roles,
            req.limit_per_role,
        )
        lap(
            "relations_pack",
            anchor_sku_id=anchor,
            raw_outfit_companion_count=raw_n,
            **pathway_log_fields(RecallPathway.SKU_RELATION_COMPAT),
        )
        groups_out: list[dict[str, Any]] = []
        for g in groups_rows:
            role = g.get("role") or ""
            rows = g.get("rows") or []
            cards = [sku_card(r) for r in rows]
            if cards:
                groups_out.append({"role": role, "skus": cards})
        lap(
            "rank_truncate",
            anchor_sku_id=anchor,
            group_count=len(groups_out),
        )
        lap("response_build", groups=len(groups_out))
        log_recommend_stage(
            tid,
            "recommend_skus_done",
            elapsed_ms=0,
            since_request_ms=int((perf_counter() - t0) * 1000),
            anchor_sku_id=anchor,
            group_count=len(groups_out),
            **pathway_log_fields(RecallPathway.SKU_RELATION_COMPAT),
        )
        total_ms = int((perf_counter() - t0) * 1000)
        self._log.log(
            "request_completed",
            "recommend_service",
            {
                "endpoint": "recommend/skus",
                "groups": len(groups_out),
                "total_ms": total_ms,
                "timings": timings,
            },
            trace_id=tid,
        )
        return {
            "anchor_sku_id": anchor,
            "groups": groups_out,
            "source_outfit_ids": list(dict.fromkeys(outfit_ids)),
        }

    def recommend_outfits(
        self,
        req: RecommendOutfitsRequest,
        *,
        trace_id: str | None = None,
        image_vec: list[float] | None = None,
    ) -> dict[str, Any]:
        tid = trace_id or uuid.uuid4().hex
        t0 = perf_counter()
        last = t0
        timings: dict[str, int] = {}

        def lap(stage: str, **kw: Any) -> None:
            nonlocal last
            now = perf_counter()
            em = int((now - last) * 1000)
            sm = int((now - t0) * 1000)
            timings[stage] = em
            last = now
            log_recommend_stage(
                tid,
                stage,
                elapsed_ms=em,
                since_request_ms=sm,
                **kw,
            )

        self._log.log(
            "request_received",
            "recommend_service",
            {"endpoint": "recommend/outfits", "q": (req.query or "")[:200]},
            trace_id=tid,
        )
        save_image_async(req.image_base64)
        # --- 图片 embedding + milvus 召回（一次调用，后续按阈值分流） ---
        anchor = None
        anchor_sim = 0.0
        image_anchor_row: dict[str, Any] | None = None
        image_candidate_rows: list[dict[str, Any]] = []
        pairs_img: list[tuple[str, float, float]] = []
        if image_vec:
            # 意图解析阶段图向量近邻：输入 sku_id/图片检索不过 up_time，仅搭配召回过滤
            pairs_img = self._sku_r.recall_by_vector(image_vec, skip_up_time=True)
            cfg_all = load_config()
            cfg_rec = cfg_all.get("recommend") or {}
            intent_cfg = cfg_all.get("intent") or {}
            recall_min_sim = float(cfg_rec.get("sku_vector_min_similarity") or 0.6)
            intent_sim_threshold = float(intent_cfg.get("image_sim_override_threshold") or 0.9)
            logger.info(
                "[意图解析·图向量近邻] recall_threshold=%.4f, intent_threshold=%.4f, "
                "召回数=%d, 结果: %s",
                recall_min_sim,
                intent_sim_threshold,
                len(pairs_img),
                ", ".join(
                    f"{sid}(sim={sim:.4f})" for sid, sim, _ in pairs_img
                ) or "(空)",
            )
            # Batch-fetch all Milvus hit SKUs in one ES mget call
            _sku_map = self._batch_get_sku_map(pairs_img)
            # A. Intent 填充：仅 top-1 sim >= intent_sim_threshold 时用图信息填充 UserIntent
            if pairs_img and pairs_img[0][1] >= intent_sim_threshold:
                anchor_sim = float(pairs_img[0][1])
                if req.image_base64:
                    image_anchor_row = _sku_map.get(pairs_img[0][0])
            # image_candidate_rows 传全部候选（intent_engine 内部按 threshold 二次过滤）
            for sid, sim, _raw in pairs_img:
                row = _sku_map.get(sid)
                if row:
                    r = dict(row)
                    r["_image_similarity"] = float(sim)
                    image_candidate_rows.append(r)

        # --- SKU ID 输入：提前获取 SKU 行数据 ---
        sku_token = find_sku_token(req.query or "")
        sku_input_row: dict[str, Any] | None = None
        if sku_token:
            sku_input_row = self._data.get_sku(sku_token)

        # --- SKU 图片 fallback：纯 SKU 输入无图片时，用 SKU 的 tryon_image 生成 embedding ---
        if not pairs_img and sku_input_row:
            _sku_img = str(sku_input_row.get("tryon_image") or "").strip()
            if _sku_img:
                logger.info(
                    "[SKU图片fallback] sku_id=%s, 使用 tryon_image 生成 embedding",
                    sku_token,
                )
                _sku_vec = embed_image_url(_sku_img)
                if _sku_vec:
                    # SKU 图片 fallback 属意图解析阶段：跳过 up_time 过滤
                    pairs_img = self._sku_r.recall_by_vector(_sku_vec, skip_up_time=True)
                    cfg_all = load_config()
                    cfg_rec = cfg_all.get("recommend") or {}
                    intent_cfg = cfg_all.get("intent") or {}
                    recall_min_sim = float(cfg_rec.get("sku_vector_min_similarity") or 0.6)
                    intent_sim_threshold = float(intent_cfg.get("image_sim_override_threshold") or 0.9)
                    logger.info(
                        "[SKU图片fallback·图向量近邻] recall_threshold=%.4f, intent_threshold=%.4f, "
                        "召回数=%d",
                        recall_min_sim, intent_sim_threshold, len(pairs_img),
                    )
                    # _fb_map 在此统一获取，避免下方 if/else 某分支未赋值导致
                    # UnboundLocalError（仅 sku_id 输入、无 image_base64 且
                    # top-1 sim >= intent_sim_threshold 时会触发）。
                    _fb_map = (
                        self._batch_get_sku_map(pairs_img) if pairs_img else {}
                    )
                    if pairs_img and pairs_img[0][1] >= intent_sim_threshold:
                        anchor_sim = float(pairs_img[0][1])
                        if req.image_base64:
                            image_anchor_row = _fb_map.get(pairs_img[0][0])
                    for sid, sim, _raw in pairs_img:
                        row = _fb_map.get(sid)
                        if row:
                            r = dict(row)
                            r["_image_similarity"] = float(sim)
                            image_candidate_rows.append(r)

        sku_anchor_row = image_anchor_row
        sku_anchor_sim = anchor_sim
        if not sku_anchor_row and sku_input_row:
            sku_anchor_row = sku_input_row
            sku_anchor_sim = 1.0

        # --- 意图解析 ---
        intent_result: IntentResult = parse_user_intent(
            req.query or "",
            image_base64=req.image_base64,
            image_anchor_row=sku_anchor_row,
            image_similarity=sku_anchor_sim,
            image_candidate_rows=image_candidate_rows,
            sku_input_row=sku_input_row,
        )
        intent = intent_result.intent

        # --- SKU ID 输入：用 SKU 属性回填 UserIntent 中仍为空的字段 ---
        if sku_input_row:
            intent = backfill_intent_from_sku(intent, sku_input_row)

        # --- 图片锚点回填：用户上传图片时，用图搜锚点 SKU 的季节补全 intent.season ---
        # 激活后续 season_conflict 过滤，避免长袖上装推荐夏季短裤等跨季节组合
        if image_anchor_row and not intent.season:
            backfilled = backfill_intent_from_sku(intent, image_anchor_row)
            if backfilled.season:
                intent = backfilled
                logger.info(
                    "[图片锚点季节回填·recommend_skus] anchor_sku=%s, season=%s",
                    image_anchor_row.get("sku_id"),
                    intent.season,
                )

        # --- 构建上传图片的锚点商品信息（意图解析完毕后立即构建） ---
        # 结构化属性统一取自意图模块融合产出的 intent_result.anchor_attrs，
        # 虚拟图锚点也带齐 category_l2/length_class，下游 length_class 预过滤不再失效。
        user_upload_anchor: dict[str, Any] | None = build_upload_anchor_row(
            anchor_attrs=intent_result.anchor_attrs,
            image_anchor_row=image_anchor_row,
            sku_anchor_sim=sku_anchor_sim,
            image_base64=req.image_base64,
            trace_id=tid,
        )

        # B. 召回候选：gender / season 与 UserIntent 一致的近邻 SKU 才作为召回 trigger
        recall_pairs: list[tuple[str, float, float]] = []
        intent_season = list(intent.season or [])
        for sid, sim, raw in pairs_img:
            if intent.gender or intent_season or intent.age:
                row = self._sku_r.get_sku(sid)
                if row and intent.gender and gender_conflict(row.get("gender"), intent.gender):
                    logger.info(
                        "[召回候选·gender过滤] sku=%s gender=%s 与 intent.gender=%s 不一致，跳过",
                        sid, row.get("gender"), intent.gender,
                    )
                    continue
                if row and intent.age and age_conflict(row.get("age"), intent.age):
                    logger.info(
                        "[召回候选·age过滤] sku=%s age=%s 与 intent.age=%s 不一致，跳过",
                        sid, row.get("age"), intent.age,
                    )
                    continue
                if row and intent_season and season_conflict(row.get("season"), intent_season):
                    logger.info(
                        "[召回候选·season过滤] sku=%s season=%s 与 intent.season=%s 无交集，跳过",
                        sid, row.get("season"), intent_season,
                    )
                    continue
            recall_pairs.append((sid, sim, raw))
        if recall_pairs:
            anchor = recall_pairs[0][0]
            anchor_sim = float(recall_pairs[0][1])

        lim = rank_outfit_limit()
        # 构建 candidate_skus：用意图过滤后的候选 SKU，批量查固定搭配库
        if intent_result.filtered_candidate_rows is not None:
            _candidate_skus = [
                str(r.get("sku_id")) for r in intent_result.filtered_candidate_rows
                if r.get("sku_id")
            ] or None
        else:
            _candidate_skus = [
                sid for sid, _sim, _raw in recall_pairs
            ] if recall_pairs else None

        compose_anchor_row: dict[str, Any] | None = None
        if user_upload_anchor:
            compose_anchor_row = user_upload_anchor
        elif not req.image_base64 and sku_input_row:
            compose_anchor_row = dict(sku_input_row)

        # --- 四路召回并行 ---
        recall_results = multi_path_recall(
            self._data,
            self._sku_r,
            intent,
            anchor,
            compose_anchor_row,
            image_base64=req.image_base64,
            trace_id=tid,
            candidate_skus=_candidate_skus,
            milvus=self._sku_r._milvus,
        )
        graph_outfits = recall_results["graph_outfits"]
        src_ids = recall_results["src_ids"]
        composed = recall_results["composed_outfits"]
        query2es_outfits = recall_results["query2es_outfits"]
        complementary_outfits = recall_results.get("complementary_outfits") or []

        # 图向量召回的搭配中，anchor item 的商品信息替换为上传图片商品信息
        if req.image_base64 and anchor and graph_outfits and user_upload_anchor:
            _replace_anchor_item_with_upload(
                graph_outfits, anchor, user_upload_anchor,
            )

        top_raw, _pw = merge_and_rank_outfits(
            graph_outfits,
            composed,
            query2es_outfits=query2es_outfits,
            complementary_outfits=complementary_outfits,
            intent=intent,
            anchor_sku_id=anchor or "",
            source_match_ids=src_ids,
            anchor_vector_sim=anchor_sim,
            limit=lim,
            trace_id=tid,
        )
        cards = [outfit_card(o) for o in top_raw]
        self._cache_outfit_cards(cards, message=req.query or "")
        total_ms = int((perf_counter() - t0) * 1000)
        self._log.log(
            "request_completed",
            "recommend_service",
            {
                "endpoint": "recommend/outfits",
                "outfits": len(cards),
                "total_ms": total_ms,
                "timings": timings,
            },
            trace_id=tid,
        )
        return {"outfits": cards}

    async def chat_stream(
        self,
        req: ChatRequest,
    ) -> AsyncIterator[dict[str, Any]]:
        trace_id = uuid.uuid4().hex
        sess = req.session_id
        t0 = perf_counter()
        last = t0
        last_sse = t0
        timings: dict[str, int] = {}

        def sse_event(payload: dict[str, Any]) -> dict[str, Any]:
            nonlocal last_sse
            now = perf_counter()
            out = dict(payload)
            out["elapsed_ms"] = int((now - last_sse) * 1000)
            last_sse = now
            return out

        def lap(stage: str, **kw: Any) -> None:
            nonlocal last
            now = perf_counter()
            em = int((now - last) * 1000)
            sm = int((now - t0) * 1000)
            timings[stage] = em
            last = now
            log_recommend_stage(
                trace_id,
                stage,
                elapsed_ms=em,
                since_request_ms=sm,
                **kw,
            )

        self._log.log(
            "request_received",
            "recommend_service",
            {
                "has_image": bool(req.image_base64),
                "msg_len": len(req.message or ""),
            },
            trace_id=trace_id,
            session_id=sess,
        )
        lap("chat_begin", session_id=sess, has_image=bool(req.image_base64))
        save_image_async(req.image_base64)
        ev0 = sse_event(
            {"type": "session_id", "session_id": sess or trace_id},
        )
        _log_chat_sse(trace_id, ev0)
        yield ev0

        # --- 图片 embedding + milvus 召回（一次调用，后续按阈值分流） ---
        # Start image embedding in background; fetch SKU input row concurrently
        from concurrent.futures import Future, ThreadPoolExecutor as _TP
        _embed_pool = _TP(max_workers=2)
        _img_embed_future: Future | None = None
        if req.image_base64:
            _img_embed_future = _embed_pool.submit(image_query_embedding, req.image_base64)

        # Fetch SKU input row while embedding runs in background
        sku_input_id = req.selected_sku_id or find_sku_token(req.message or "")
        sku_input_row: dict[str, Any] | None = None
        if sku_input_id:
            sku_input_row = self._data.get_sku(sku_input_id)

        # Now collect the image embedding result
        vec: list[float] | None = None
        if _img_embed_future is not None:
            vec = _img_embed_future.result()
        _embed_pool.shutdown(wait=False)
        lap("embedding_query", image_vector_dim=len(vec) if vec else 0)

        pairs_img: list[tuple[str, float, float]] = []
        image_anchor_row: dict[str, Any] | None = None
        image_candidate_rows: list[dict[str, Any]] = []
        anchor_sim = 0.0
        if vec and req.image_base64:
            # 意图解析阶段图向量近邻：输入 sku_id/图片检索不过 up_time，仅搭配召回过滤
            pairs_img = self._sku_r.recall_by_vector(vec, skip_up_time=True)
            cfg_all = load_config()
            cfg_rec = cfg_all.get("recommend") or {}
            intent_cfg = cfg_all.get("intent") or {}
            recall_min_sim = float(cfg_rec.get("sku_vector_min_similarity") or 0.6)
            intent_sim_threshold = float(intent_cfg.get("image_sim_override_threshold") or 0.9)
            # Batch-fetch all Milvus hit SKUs in one ES mget call
            _chat_sku_map = self._batch_get_sku_map(pairs_img)
            # A. Intent 填充：仅 top-1 sim >= intent_sim_threshold 时用图信息填充 UserIntent
            if pairs_img and pairs_img[0][1] >= intent_sim_threshold:
                image_anchor_row = _chat_sku_map.get(pairs_img[0][0])
                anchor_sim = float(pairs_img[0][1])
            # image_candidate_rows 传全部候选（intent_engine 内部按 threshold 二次过滤）
            for sid, sim, _raw in pairs_img:
                row = _chat_sku_map.get(sid)
                if row:
                    r = dict(row)
                    r["_image_similarity"] = float(sim)
                    image_candidate_rows.append(r)
            lap(
                "milvus_recall",
                milvus_hit_count=len(pairs_img),
                recall_min_similarity=recall_min_sim,
                intent_sim_threshold=intent_sim_threshold,
                hits=[
                    {"sku_id": sid, "similarity": round(sim, 4)}
                    for sid, sim, _raw in pairs_img
                ],
                note="image_multimodal_for_anchor",
            )

        # --- SKU 图片 fallback：纯 SKU 输入无图片时，用 SKU 的 tryon_image 生成 embedding ---
        if not vec and sku_input_row:
            _sku_img = str(sku_input_row.get("tryon_image") or "").strip()
            if _sku_img:
                logger.info(
                    "[SKU图片fallback] sku_id=%s, 使用 tryon_image 生成 embedding",
                    sku_input_id,
                )
                vec = embed_image_url(_sku_img)
                if vec:
                    # SKU 图片 fallback 属意图解析阶段：跳过 up_time 过滤
                    pairs_img = self._sku_r.recall_by_vector(vec, skip_up_time=True)
                    cfg_all = load_config()
                    cfg_rec = cfg_all.get("recommend") or {}
                    intent_cfg = cfg_all.get("intent") or {}
                    recall_min_sim = float(cfg_rec.get("sku_vector_min_similarity") or 0.6)
                    intent_sim_threshold = float(intent_cfg.get("image_sim_override_threshold") or 0.9)
                    if pairs_img and pairs_img[0][1] >= intent_sim_threshold:
                        _fb2_map = self._batch_get_sku_map(pairs_img)
                        image_anchor_row = _fb2_map.get(pairs_img[0][0])
                        anchor_sim = float(pairs_img[0][1])
                    else:
                        _fb2_map = self._batch_get_sku_map(pairs_img)
                    for sid, sim, _raw in pairs_img:
                        row = _fb2_map.get(sid)
                        if row:
                            r = dict(row)
                            r["_image_similarity"] = float(sim)
                            image_candidate_rows.append(r)
                    lap(
                        "milvus_recall_sku_img",
                        milvus_hit_count=len(pairs_img),
                        sku_image_source="tryon_image",
                        hits=[
                            {"sku_id": sid, "similarity": round(sim, 4)}
                            for sid, sim, _raw in pairs_img
                        ],
                    )

        # 当有 SKU 行数据但无图搜结果时，将 SKU 行作为 image_anchor_row 传入意图解析，
        # 使 extract_intent 内部能用 SKU 属性填充 slots，正确判断 LLM fallback
        sku_anchor_row = image_anchor_row
        sku_anchor_sim = anchor_sim
        if not sku_anchor_row and sku_input_row:
            sku_anchor_row = sku_input_row
            sku_anchor_sim = 1.0

        # --- 意图解析（轻量 Trie + 图搜 slots 合并 + LLM fallback） ---
        intent_result: IntentResult = parse_user_intent(
            req.message or "",
            image_base64=req.image_base64,
            image_anchor_row=sku_anchor_row,
            image_similarity=sku_anchor_sim,
            image_candidate_rows=image_candidate_rows,
            model_override=req.llm_model,
            sku_input_row=sku_input_row,
        )
        intent = intent_result.intent

        # --- SKU ID 输入：用 SKU 属性回填 UserIntent 中仍为空的字段 ---
        if sku_input_row:
            intent = backfill_intent_from_sku(intent, sku_input_row)
            intent_result = IntentResult(
                intent=intent,
                method=intent_result.method,
                confidence=intent_result.confidence,
                slots_detail=intent_result.slots_detail,
                llm_fallback=intent_result.llm_fallback,
                image_override=intent_result.image_override,
                image_override_slots=intent_result.image_override_slots,
                filtered_candidate_rows=intent_result.filtered_candidate_rows,
                anchor_source=intent_result.anchor_source,
                image_role=intent_result.image_role,
            )
            logger.info(
                "[SKU输入回填] sku_id=%s, role=%s, gender=%s, season=%s",
                sku_input_id,
                intent.anchor_role,
                intent.gender,
                intent.season,
            )

        # --- 图片锚点回填：用户上传图片时，用图搜锚点 SKU 的季节补全 intent.season ---
        # 激活后续 season_conflict 过滤，避免长袖上装推荐夏季短裤等跨季节组合
        if image_anchor_row and not intent.season:
            backfilled = backfill_intent_from_sku(intent, image_anchor_row)
            if backfilled.season:
                intent = backfilled
                intent_result = IntentResult(
                    intent=intent,
                    method=intent_result.method,
                    confidence=intent_result.confidence,
                    slots_detail=intent_result.slots_detail,
                    llm_fallback=intent_result.llm_fallback,
                    image_override=intent_result.image_override,
                    image_override_slots=intent_result.image_override_slots,
                    filtered_candidate_rows=intent_result.filtered_candidate_rows,
                    anchor_source=intent_result.anchor_source,
                    image_role=intent_result.image_role,
                )
                logger.info(
                    "[图片锚点季节回填] anchor_sku=%s, season=%s",
                    image_anchor_row.get("sku_id"),
                    intent.season,
                )

        lap(
            "intent_extract",
            query_type=intent.query_type,
            anchor_role=intent.anchor_role,
            method=intent_result.method,
            confidence=intent_result.confidence,
        )

        ev_intent = sse_event({
            "type": "intent",
            "intent": intent.model_dump(),
            **intent_result.to_sse_fields(),
        })
        _log_chat_sse(trace_id, ev_intent)
        yield ev_intent

        # --- 构建上传图片的锚点商品信息（意图解析完毕后立即构建） ---
        # 结构化属性统一取自意图模块融合产出的 intent_result.anchor_attrs。
        user_upload_anchor: dict[str, Any] | None = build_upload_anchor_row(
            anchor_attrs=intent_result.anchor_attrs,
            image_anchor_row=image_anchor_row,
            sku_anchor_sim=sku_anchor_sim,
            image_base64=req.image_base64,
            trace_id=trace_id,
        )

        # B. 召回候选：gender / season 与 UserIntent 一致的近邻 SKU 才作为召回 trigger
        recall_pairs: list[tuple[str, float, float]] = []
        intent_season = list(intent.season or [])
        for sid, sim, raw in pairs_img:
            if intent.gender or intent_season or intent.age:
                row = self._sku_r.get_sku(sid)
                if row and intent.gender and gender_conflict(row.get("gender"), intent.gender):
                    logger.info(
                        "[召回候选·gender过滤] sku=%s gender=%s 与 intent.gender=%s 不一致，跳过",
                        sid, row.get("gender"), intent.gender,
                    )
                    continue
                if row and intent.age and age_conflict(row.get("age"), intent.age):
                    logger.info(
                        "[召回候选·age过滤] sku=%s age=%s 与 intent.age=%s 不一致，跳过",
                        sid, row.get("age"), intent.age,
                    )
                    continue
                if row and intent_season and season_conflict(row.get("season"), intent_season):
                    logger.info(
                        "[召回候选·season过滤] sku=%s season=%s 与 intent.season=%s 无交集，跳过",
                        sid, row.get("season"), intent_season,
                    )
                    continue
            recall_pairs.append((sid, sim, raw))

        # --- 锚点解析 ---
        anchor_path = RecallPathway.ANCHOR_NONE
        anchor = self._resolve_anchor_sku(req, intent)
        if anchor:
            anchor_path = RecallPathway.ANCHOR_EXPLICIT
        elif recall_pairs:
            anchor = recall_pairs[0][0]
            anchor_path = RecallPathway.ANCHOR_SKU_VECTOR

        anchor_skus: list[dict[str, Any]] = []
        if recall_pairs:
            anchor_sim = float(recall_pairs[0][1]) if recall_pairs else 0.0
            _anchor_ids = [sid for sid, _sim, _dist in recall_pairs[:5] if sid]
            _anchor_map = {
                str(r.get("sku_id") or ""): r
                for r in self._data.get_skus(_anchor_ids)
                if r
            }
            for sid, sim, dist in recall_pairs[:5]:
                row = _anchor_map.get(sid)
                if row:
                    anchor_skus.append(
                        {
                            "sku_id": sid,
                            "title": row.get("title"),
                            "similarity": float(sim),
                            "milvus_raw": float(dist),
                            "display_image": row.get("display_image") or "",
                            "tryon_image": row.get("tryon_image") or "",
                        },
                    )
        elif anchor:
            row = self._sku_r.get_sku(anchor)
            if row:
                anchor_skus.append(
                    {
                        "sku_id": anchor,
                        "title": row.get("title"),
                        "similarity": 1.0,
                        "display_image": row.get("display_image") or "",
                        "tryon_image": row.get("tryon_image") or "",
                    },
                )
        if not pairs_img and not anchor:
            lap("milvus_recall", milvus_hit_count=0, note="no_vec_no_anchor")
        log_recommend_stage(
            trace_id,
            "chat_anchor_final",
            elapsed_ms=0,
            since_request_ms=int((perf_counter() - t0) * 1000),
            anchor_sku_id=anchor,
            anchor_candidate_count=len(anchor_skus),
            **pathway_log_fields(anchor_path),
        )
        ev_anchor = sse_event(
            {"type": "anchor_skus", "skus": anchor_skus},
        )
        _log_chat_sse(
            trace_id,
            ev_anchor,
            recall_extra=pathway_log_fields(anchor_path),
        )
        yield ev_anchor

        cfg = load_config()
        lim = rank_outfit_limit(cfg)
        outfit_payload: dict[str, Any] = {"outfits": []}
        sku_path = RecallPathway.SKU_SKIPPED_NO_ANCHOR

        anchor_sim = 0.0
        if anchor_skus:
            anchor_sim = float(anchor_skus[0].get("similarity") or 0.0)

        compose_anchor_row: dict[str, Any] | None = None
        if user_upload_anchor:
            compose_anchor_row = user_upload_anchor
        elif not req.image_base64 and sku_input_row:
            compose_anchor_row = dict(sku_input_row)

        # --- 三路召回并行 ---
        # 构建 candidate_skus：用意图过滤后的候选 SKU，批量查固定搭配库
        if intent_result.filtered_candidate_rows is not None:
            _candidate_skus = [
                str(r.get("sku_id")) for r in intent_result.filtered_candidate_rows
                if r.get("sku_id")
            ] or None
        else:
            _candidate_skus = [
                sid for sid, _sim, _raw in recall_pairs
            ] if recall_pairs else None
        recall_results = multi_path_recall(
            self._data,
            self._sku_r,
            intent,
            anchor,
            compose_anchor_row,
            image_base64=req.image_base64,
            trace_id=trace_id,
            model_override=req.llm_model,
            candidate_skus=_candidate_skus,
            milvus=self._sku_r._milvus,
        )
        graph_outfits = recall_results["graph_outfits"]
        src_ids = recall_results["src_ids"]
        composed_outfits = recall_results["composed_outfits"]
        query2es_outfits = recall_results["query2es_outfits"]
        complementary_outfits = recall_results.get("complementary_outfits") or []
        es_debug = recall_results.get("es_debug") or {}
        recall_timings = recall_results["timings"]

        # 图向量召回的搭配中，anchor item 的商品信息替换为上传图片商品信息
        if req.image_base64 and anchor and graph_outfits and user_upload_anchor:
            _replace_anchor_item_with_upload(
                graph_outfits, anchor, user_upload_anchor,
            )

        lap(
            "multi_path_recall",
            recall_timings=recall_timings,
            compose_mode=recall_results.get("compose_mode"),
            recalled_sku_count=recall_results.get("recalled_sku_count", 0),
            composed_outfit_count=len(composed_outfits),
            graph_outfit_count=len(graph_outfits),
            query2es_outfit_count=len(query2es_outfits),
            complementary_outfit_count=len(complementary_outfits),
        )

        # SSE: 各路召回进度（global 模式下三路合成通路 count 为 SKU 数，unit=skus；
        # image_vector 恒为固定搭配数，unit=outfits）
        for path_name in ("image_vector", "text_vector", "query2es", "complementary_model"):
            pt = recall_timings.get(path_name)
            if pt:
                yield sse_event({
                    "type": "recall_progress",
                    "path": path_name,
                    "status": "done",
                    "count": pt.get("count", 0),
                    "unit": pt.get("unit", "outfits"),
                    "elapsed_ms": pt.get("elapsed_ms", 0),
                })

        # SSE: ES 调试信息
        if es_debug:
            _cfg = load_config()
            _es_host = get_elasticsearch_hosts(_cfg)[0] if get_elasticsearch_hosts(_cfg) else "http://127.0.0.1:9200"
            _es_index = get_elasticsearch_indices(_cfg).get("skus", "")
            es_queries_for_sse: list[dict[str, Any]] = []
            for role, meta in es_debug.items():
                es_queries_for_sse.append({
                    "role": role,
                    "source": meta.get("source", ""),
                    "hits": meta.get("hits"),
                    "es_query": meta.get("es_query", {}),
                })
            yield sse_event({
                "type": "es_debug",
                "queries": es_queries_for_sse,
                "es_host": _es_host,
                "es_index": _es_index,
            })

        before_dedupe = (
            len(graph_outfits) + len(composed_outfits) + len(query2es_outfits)
            + len(complementary_outfits)
        )
        # --- 第一步：合并去重（属于召回阶段） ---
        deduped, _vec_map, outfit_path = merge_and_dedupe_outfits(
            graph_outfits,
            composed_outfits,
            query2es_outfits=query2es_outfits,
            complementary_outfits=complementary_outfits,
            anchor_sku_id=anchor or "",
            source_match_ids=src_ids,
            anchor_vector_sim=anchor_sim,
        )
        lap(
            "outfit_dedupe",
            before=before_dedupe,
            after=len(deduped),
            anchor_sku_id=anchor,
        )

        # SSE: 召回完成（去重后、排序前）
        _pool_debug = recall_results.get("pool_debug") or {}
        yield sse_event({
            "type": "recall_done",
            "mode": recall_results.get("compose_mode", "per_channel"),
            "recalled_sku_count": recall_results.get("recalled_sku_count", 0),
            "composed_outfit_count": recall_results.get("composed_outfit_count", 0),
            "multi_channel_hits": _pool_debug.get("multi_channel_hits_total", 0),
            "roles": recall_results.get("pool_role_counts") or {},
            "before_dedupe": before_dedupe,
            "after_dedupe": len(deduped),
        })

        # 交出控制权，让事件循环将 recall_done flush 到客户端
        await asyncio.sleep(0)

        # --- 粗排：规则打分截断候选集 ---
        yield sse_event({"type": "coarse_rank_start"})
        await asyncio.sleep(0)

        _t_coarse = perf_counter()
        coarse_ranked = await asyncio.to_thread(
            coarse_rank_outfits,
            deduped,
            intent=intent,
            source_match_ids=src_ids,
            trace_id=trace_id,
        )
        _coarse_elapsed_ms = int((perf_counter() - _t_coarse) * 1000)
        lap(
            "coarse_rank",
            input_count=len(deduped),
            output_count=len(coarse_ranked),
            coarse_elapsed_ms=_coarse_elapsed_ms,
        )

        yield sse_event({
            "type": "coarse_rank_done",
            "input_count": len(deduped),
            "output_count": len(coarse_ranked),
            "elapsed_ms": _coarse_elapsed_ms,
        })
        await asyncio.sleep(0)

        # --- 第二步：排序打分 + 推荐理由（合并阶段） ---
        # enable_llm_rank_reason 开启时，LLM 排序同时生成 reason，不再单独调 reason LLM
        rec_cfg = cfg.get("recommend") or {}
        if req.enable_llm_rank_reason is not None:
            enable_llm_rank_reason = bool(req.enable_llm_rank_reason)
        else:
            # 兼容旧字段：同时看 ranking_scoring_method 和 skip_reason
            if req.ranking_scoring_method is not None or req.skip_reason is not None:
                enable_llm_rank_reason = False  # 旧模式，走下方兼容逻辑
            else:
                enable_llm_rank_reason = bool(rec_cfg.get("enable_llm_rank_reason", False))

        if enable_llm_rank_reason:
            scoring_method_override = "llm"
        else:
            scoring_method_override = req.ranking_scoring_method if req.ranking_scoring_method else None

        # SSE: 排序和理由开始
        yield sse_event({"type": "ranking_reason_start"})
        await asyncio.sleep(0)

        # 拆分模式（partner_qwen / partner_vision）：LLM 排序走本地 partner vLLM，
        # 推荐理由另起 LLM 并行生成，两步在请求级 asyncio.gather 并发。
        #   partner_qwen  : 理由走 qwen3.5-flash 文本模型（generate_outfit_reason_payload）
        #   partner_vision: 理由走 qwen3.6-27b(vision_llm) 看单品 tryon_image（vision_reasoner）
        # 理由对整套 coarse 生成（精排会重排，幸存者未必是 coarse 前 N），按
        # outfit_reason_key(=outfit_id) 写回幸存者。req.llm_model 两步都不转发：
        # partner 防 vLLM 404、reason 强制 config 对应模型。
        _rank_method_sel = str(rec_cfg.get("llm_rank_reason_method") or "ranking_llm")
        _split_rank_reason = enable_llm_rank_reason and _rank_method_sel in (
            "partner_qwen", "partner_vision",
        )
        _reason_fn = (
            generate_outfit_reason_payload_vision
            if _rank_method_sel == "partner_vision"
            else generate_outfit_reason_payload
        )

        if _split_rank_reason:
            coarse_cards = [outfit_card(o) for o in coarse_ranked]
            top_raw, reason_pay = await asyncio.gather(
                asyncio.to_thread(
                    rank_deduped_outfits,
                    coarse_ranked,
                    intent=intent,
                    source_match_ids=src_ids,
                    limit=lim,
                    trace_id=trace_id,
                    scoring_method_override=scoring_method_override,
                    model_override=None,
                ),
                asyncio.to_thread(
                    _reason_fn,
                    req.message or "",
                    coarse_cards,
                    model_override=None,
                    limit=len(coarse_cards),
                ),
            )
        else:
            top_raw = await asyncio.to_thread(
                rank_deduped_outfits,
                coarse_ranked,
                intent=intent,
                source_match_ids=src_ids,
                limit=lim,
                trace_id=trace_id,
                scoring_method_override=scoring_method_override,
                model_override=req.llm_model,
            )
            reason_pay = None
        # 从排序结果中提取排序耗时与打分方式
        _ranking_elapsed_ms = 0
        _ranking_method = "rule"
        if top_raw:
            _ranking_elapsed_ms = int(top_raw[0].get("_ranking_elapsed_ms") or 0)
            _ranking_method = str(top_raw[0].get("_ranking_scoring_method") or "rule")
        lap(
            "outfit_ranking",
            input_count=len(coarse_ranked),
            output_count=len(top_raw),
            scoring_method=_ranking_method,
            ranking_elapsed_ms=_ranking_elapsed_ms,
            split_rank_reason=_split_rank_reason,
        )

        # SSE: 排序完成
        yield sse_event({
            "type": "ranking_reason_done",
            "input_count": len(coarse_ranked),
            "output_count": len(top_raw),
            "scoring_method": _ranking_method,
            "ranking_elapsed_ms": _ranking_elapsed_ms,
            "split_rank_reason": _split_rank_reason,
        })

        # 交出控制权，让事件循环将 ranking_reason_done flush 到客户端
        await asyncio.sleep(0)

        # 推荐理由写回 raw outfit 的 reason 字段，outfit_card() 会自动拷贝。
        if _split_rank_reason:
            # 拆分模式：优先用 qwen3.5-flash 理由；缺失时回退 partner 评语，再缺失留空
            per_outfit_reasons = (reason_pay or {}).get("outfit_reasons") or {}
            _reason_chars = 0
            for o in top_raw:
                key = outfit_reason_key(outfit_card(o))
                qwen_reason = str(per_outfit_reasons.get(key) or "").strip()
                if qwen_reason:
                    o["reason"] = qwen_reason
                    _reason_chars += len(qwen_reason)
                else:
                    fb = str(o.get("_llm_reason") or "").strip()
                    if fb:
                        o["reason"] = fb
                        _reason_chars += len(fb)
            lap(
                "reason_llm",
                reason_chars=_reason_chars,
                outfit_reason_count=len(top_raw),
                split=True,
            )
        elif enable_llm_rank_reason:
            # 合并模式（partner / ranking_llm）：reason 已在 LLM 排序中一并生成
            for o in top_raw:
                llm_reason = str(o.get("_llm_reason") or "").strip()
                if llm_reason:
                    o["reason"] = llm_reason

        outfit_payload = {
            "outfits": [outfit_card(o) for o in top_raw],
        }
        self._cache_outfit_cards(
            outfit_payload["outfits"], message=req.message or "",
        )

        # --- 在线缺陷检测（config 控制） ---
        rec_cfg_inner = cfg.get("recommend") or {}
        if rec_cfg_inner.get("defect_check_online", False):
            try:
                # 预取本批 outfit 涉及的 SKU 行（走 ES），构建 sku_store 字典
                _defect_sku_ids: set[str] = set()
                for o in top_raw:
                    if not isinstance(o, dict):
                        continue
                    for it in o.get("items") or []:
                        if not isinstance(it, dict):
                            continue
                        _sid = str(
                            it.get("sku_id")
                            or it.get("skuId")
                            or it.get("attrAlias")
                            or it.get("idAlias")
                            or ""
                        ).strip()
                        if _sid:
                            _defect_sku_ids.add(_sid)
                if anchor:
                    _defect_sku_ids.add(str(anchor))
                _sku_store_map = {
                    str(r.get("sku_id") or ""): r
                    for r in self._data.get_skus(list(_defect_sku_ids))
                    if r
                }
                _analyzer = OutfitDefectAnalyzer(sku_store=_sku_store_map)
                _anchor_row = _sku_store_map.get(str(anchor or ""))
                _intent_dict = intent.model_dump() if intent else {}
                _defect_reports = [
                    _analyzer.analyze(o, _intent_dict, _anchor_row)
                    for o in top_raw
                    if isinstance(o, dict)
                ]
                _defect_summary = summarize_defects(_defect_reports)
                self._log.log(
                    "defect_analysis",
                    "recommend_service",
                    {
                        "outfit_count": len(top_raw),
                        "defect_summary": _defect_summary,
                        "details": [
                            r.to_dict() for r in _defect_reports if r.has_defects
                        ],
                    },
                    trace_id=trace_id,
                    session_id=sess,
                )
                logger.info(
                    "[在线缺陷检测] outfits=%d, defects=%d, rate=%.1f%%",
                    _defect_summary["total_outfits"],
                    _defect_summary["total_defect_count"],
                    _defect_summary["defect_rate"] * 100,
                )
            except Exception:
                logger.warning("在线缺陷检测异常，已跳过", exc_info=True)

        log_recommend_stage(
            trace_id,
            "chat_multi_recall_merged",
            elapsed_ms=0,
            since_request_ms=int((perf_counter() - t0) * 1000),
            anchor_sku_id=anchor,
            graph_count=len(graph_outfits),
            composed_count=len(composed_outfits),
            query2es_count=len(query2es_outfits),
            return_count=len(top_raw),
            recall_timings=recall_timings,
            **pathway_log_fields(outfit_path),
        )
        log_recommend_stage(
            trace_id,
            "chat_branch_recall",
            elapsed_ms=0,
            since_request_ms=int((perf_counter() - t0) * 1000),
            **chat_recall_pathway_bundle(
                anchor_path,
                outfit_path,
                sku_path,
            ),
        )

        lap(
            "rank_truncate",
            outfit_count=len(outfit_payload.get("outfits") or []),
        )
        t_reason = perf_counter()
        skip_reason = True  # 默认跳过
        if enable_llm_rank_reason and not _split_rank_reason:
            # 合并模式：reason 已在 LLM 排序中一并生成，无需独立调用
            # （拆分模式 _split_rank_reason 已在上文单独 lap 过 reason_llm）
            lap(
                "reason_llm",
                reason_chars=sum(len(oc.get("reason") or "") for oc in (outfit_payload.get("outfits") or [])),
                outfit_reason_count=len(top_raw),
                merged=True,
            )
        else:
            if req.skip_reason is not None:
                skip_reason = bool(req.skip_reason)
            else:
                skip_reason = bool(rec_cfg.get("skip_reason", True))

        if not enable_llm_rank_reason:
            if skip_reason:
                lap(
                    "reason_llm",
                    reason_chars=0,
                    outfit_reason_count=0,
                    skipped=True,
                )
            else:
                pay = await asyncio.to_thread(
                    generate_outfit_reason_payload,
                    req.message or "",
                    outfit_payload.get("outfits") or [],
                    model_override=req.llm_model,
                )
                per_outfit_reasons = pay.get("outfit_reasons") or {}
                ir = pay.get("item_reasons") or {}
                for oc in outfit_payload.get("outfits") or []:
                    key = outfit_reason_key(oc)
                    for it in oc.get("items") or []:
                        sid = str(it.get("sku_id") or "")
                        if sid in ir:
                            it["reason"] = ir[sid]
                    if key and key in per_outfit_reasons:
                        oc["reason"] = str(per_outfit_reasons[key])
                    else:
                        item_parts = [
                            str(it.get("reason") or "")
                            for it in (oc.get("items") or [])
                            if it.get("reason")
                        ]
                        if item_parts:
                            oc["reason"] = " ".join(item_parts)[:500]
                lap(
                    "reason_llm",
                    reason_chars=len(str(pay.get("outfit_reason") or "")),
                    outfit_reason_count=len(per_outfit_reasons),
                )
        log_recommend_stage(
            trace_id,
            "chat_outfit_built",
            elapsed_ms=0,
            since_request_ms=int((perf_counter() - t0) * 1000),
            outfit_count=len(outfit_payload.get("outfits") or []),
            **chat_recall_pathway_bundle(anchor_path, outfit_path, sku_path),
        )

        # --- 虚拟试穿 ---
        tryon_cfg = rec_cfg.get("tryon") or {}
        if req.enable_tryon is not None:
            do_tryon = bool(req.enable_tryon)
        else:
            do_tryon = bool(tryon_cfg.get("enabled", False))
        logger.info(
            "tryon 决策: enable_tryon=%s, cfg_enabled=%s, do_tryon=%s",
            req.enable_tryon, tryon_cfg.get("enabled"), do_tryon,
        )

        if do_tryon:
            yield sse_event({"type": "tryon_progress", "status": "running"})
            await asyncio.sleep(0)

            tryon_gender = intent.gender or "male"
            replace_existing = bool(tryon_cfg.get("replace_existing_image", False))

            tryon_results = await asyncio.to_thread(
                batch_tryon_outfits,
                outfit_payload["outfits"],
                tryon_gender,
                cfg,
                replace_existing=replace_existing,
                person_image_override=req.tryon_person_image,
            )

            tryon_map = {
                r["outfit_id"]: r["tryon_image"]
                for r in tryon_results
                if r.get("tryon_image")
            }
            for oc in outfit_payload["outfits"]:
                oid = oc.get("outfit_id")
                if oid in tryon_map:
                    oc["outfit_tryon_image"] = tryon_map[oid]

            tryon_ok = sum(1 for r in tryon_results if r.get("status") == "success")
            tryon_failed = sum(1 for r in tryon_results if r.get("status") == "failed")
            tryon_skipped = sum(1 for r in tryon_results if r.get("status") == "skipped")
            tryon_detail = [
                {
                    "outfit_id": r.get("outfit_id"),
                    "status": r.get("status"),
                    "reason": r.get("reason") or "",
                }
                for r in tryon_results
                if r.get("status") != "success"
            ]
            yield sse_event({
                "type": "tryon_progress",
                "status": "done",
                "success_count": tryon_ok,
                "total_count": len(outfit_payload["outfits"]),
            })
            lap(
                "tryon",
                success=tryon_ok,
                failed=tryon_failed,
                skipped=tryon_skipped,
                total=len(outfit_payload["outfits"]),
                detail=tryon_detail,
            )
        else:
            lap("tryon", skipped=True)

        ev_out = sse_event(
            {
                "type": "outfit_results",
                "outfits": outfit_payload["outfits"],
            },
        )
        _log_chat_sse(
            trace_id,
            ev_out,
            recall_extra=pathway_log_fields(outfit_path),
        )
        yield ev_out

        log_recommend_stage(
            trace_id,
            "chat_reason_generated",
            elapsed_ms=int((perf_counter() - t_reason) * 1000),
            since_request_ms=int((perf_counter() - t0) * 1000),
        )
        if enable_llm_rank_reason:
            # reason 已嵌入 outfit cards，生成一段总述文本
            reason_parts = [
                str(oc.get("reason") or "")
                for oc in (outfit_payload.get("outfits") or [])
                if oc.get("reason")
            ]
            reason_text = "\n\n".join(reason_parts[:3]) if reason_parts else ""
        elif skip_reason:
            reason_text = ""
        else:
            reason_text = str(pay.get("outfit_reason") or "")
            if not reason_text and pay.get("item_reasons"):
                parts = list((pay.get("item_reasons") or {}).values())
                reason_text = " ".join(p for p in parts if p)[:800]
        if not reason_text:
            reason_text = (
                "以上搭配来自固定搭配库与文本向量临时组合，可按需挑选。"
            )
        ev_txt = sse_event({"type": "text", "content": reason_text})
        _log_chat_sse(trace_id, ev_txt)
        yield ev_txt
        lap("response_build", done=True)
        total_ms = int((perf_counter() - t0) * 1000)
        ev_done = sse_event({"type": "done", "total_ms": total_ms})
        _log_chat_sse(trace_id, ev_done)
        yield ev_done

        log_recommend_stage(
            trace_id,
            "chat_stream_complete",
            elapsed_ms=0,
            since_request_ms=total_ms,
        )
        self._log.log(
            "request_completed",
            "recommend_service",
            {
                "endpoint": "chat",
                "total_ms": total_ms,
                "timings": timings,
                **chat_recall_pathway_bundle(anchor_path, outfit_path, sku_path),
            },
            trace_id=trace_id,
            session_id=sess,
        )
        self._log.dump_replay(
            trace_id,
            {
                "intent": intent.model_dump(),
                "intent_method": intent_result.method,
                "intent_confidence": intent_result.confidence,
                "anchor": anchor,
                "outfits": outfit_payload,
                "recall_timings": recall_timings,
                "timings": timings,
                "total_ms": total_ms,
                "recall_pathways": {
                    "anchor": anchor_path.value,
                    "outfit": outfit_path.value,
                },
                "recall_pathway_labels": {
                    "anchor": RECALL_PATHWAY_LABELS[anchor_path],
                    "outfit": RECALL_PATHWAY_LABELS[outfit_path],
                },
            },
        )

    def regenerate_outfit_reason(
        self,
        req: RegenerateReasonRequest,
    ) -> dict[str, Any]:
        """根据缓存的 outfit card 重新生成推荐理由。"""
        cached = self._outfit_cache.get(req.outfit_id)
        if not cached:
            return {"error": "outfit not found in cache", "outfit_id": req.outfit_id}

        card = cached["card"]
        summary = req.message if req.message is not None else cached.get("message", "")

        mode = _reason_mode()
        key, outfit_reason, item_reasons = _reason_one_outfit(
            summary, card, mode, model_override=req.llm_model,
        )

        return {
            "outfit_id": req.outfit_id,
            "reason": outfit_reason,
            "item_reasons": item_reasons,
        }

    # ── 对外接口（按 docs/FILA穿搭推荐入参出参.md）──

    async def external_recommend(
        self,
        req: ExternalRecommendRequest,
        *,
        trace_id: str | None = None,
        app_id: str | None = None,
        caller: str | None = None,
    ) -> dict[str, Any]:
        """对外搭配推荐：复用 chat_stream 引擎，reshape 为文档出参 + 审计落库。"""
        t0 = perf_counter()
        session_id = (req.session_id or "").strip() or uuid.uuid4().hex
        # 入参 strip 一次：ES 查询与响应回显统一使用 strip 后的 sku_id，
        # 避免调用方误以为返回的 sku 与入参完全一致（SS-02）。
        input_sku_id = (req.input_sku_id or "").strip()
        image_base64: str | None = None
        if req.image_url:
            image_base64 = fetch_image_url_to_base64(req.image_url)
            if image_base64 is None:
                logger.warning(
                    "[对外接口] image_url 抓取失败，降级为仅用 input_sku_id 锚点: %s",
                    (req.image_url or "")[:120],
                )

        chat_req = ChatRequest(
            session_id=session_id,
            message=req.message or "",
            image_base64=image_base64,
            selected_sku_id=input_sku_id or None,
            enable_tryon=bool(req.tryon),
            skip_reason=False,  # 文档要求每套返回 reason
        )

        outfits: list[dict[str, Any]] = []
        captured: dict[str, Any] = {"recall_progress": []}
        status = "ok"
        error: str | None = None
        outfits_out: list[dict[str, Any]] = []
        try:
            async for ev in self.chat_stream(chat_req):
                et = str(ev.get("type") or "")
                if et == "outfit_results":
                    outfits = list(ev.get("outfits") or [])
                elif et == "intent":
                    captured["intent"] = ev
                elif et == "anchor_skus":
                    captured["anchor_skus"] = ev
                elif et == "recall_done":
                    captured["recall_done"] = ev
                elif et == "ranking_reason_done":
                    captured["ranking_reason_done"] = ev
                elif et == "recall_progress":
                    captured["recall_progress"].append(ev)
            out = reshape_outfits_to_external(
                outfits,
                input_sku_id=input_sku_id,
                image_url=req.image_url,
                session_id=session_id,
                data_facade=self._data,
            )
            outfits_out = list(out.get("outfits") or [])
            return out
        except Exception as exc:
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._write_recommend_audit(
                req, image_base64, session_id, captured, status, error,
                trace_id, app_id, caller, t0, outfits_out,
            )

    def _write_recommend_audit(
        self,
        req: ExternalRecommendRequest,
        image_base64: str | None,
        session_id: str,
        captured: dict[str, Any],
        status: str,
        error: str | None,
        trace_id: str | None,
        app_id: str | None,
        caller: str | None,
        t0: float,
        outfits_out: list[dict[str, Any]],
    ) -> None:
        """拼 recommend 审计文档并写 ES；关闭/失败均静默。"""
        if not self._audit.enabled:
            return
        try:
            input_block = build_input_block(
                input_sku_id=(req.input_sku_id or "").strip(),
                image_url=req.image_url,
                image_base64=image_base64,
                message=req.message,
                tryon=req.tryon,
                reason_style=req.reason_style,
            )
            captured["outfits"] = outfits_out
            meta = {
                "trace_id": trace_id,
                "session_id": session_id,
                "app_id": app_id,
                "caller": caller,
                "ts": now_iso(),
                "elapsed_ms": int((perf_counter() - t0) * 1000),
                "status": status,
                "error": error,
            }
            doc = build_recommend_doc(
                input_block=input_block, captured=captured, meta=meta,
            )
            self._audit.write(doc)
        except Exception:  # noqa: BLE001
            logger.warning("request audit (recommend) failed", exc_info=True)

    def external_regenerate(
        self,
        req: ExternalRegenerateReasonRequest,
        *,
        trace_id: str | None = None,
        app_id: str | None = None,
        caller: str | None = None,
    ) -> dict[str, Any]:
        """对外重新生成理由：缓存命中复用现有路径；miss 时 ES 兜底 + 审计落库。

        返回 ``{"outfit_id", "reason"}``（丢弃 item_reasons）。
        reason_style 暂透传不接入。
        """
        t0 = perf_counter()
        status = "ok"
        error: str | None = None
        result: dict[str, Any] | None = None
        try:
            # 1) 优先走缓存（与 /regenerate-reason 同路径）
            cached = self._outfit_cache.get(req.outfit_id)
            if cached:
                r = self.regenerate_outfit_reason(
                    RegenerateReasonRequest(outfit_id=req.outfit_id),
                )
                if "error" not in r:
                    result = {
                        "outfit_id": req.outfit_id,
                        "reason": r.get("reason") or "",
                    }
                    return result

            # 2) 缓存 miss / 失败 → ES 取 outfit 兜底重建 card 再生成理由
            try:
                raw_outfit = self._data.get_outfit(req.outfit_id)
                if not raw_outfit:
                    result = {"error": "outfit not found", "outfit_id": req.outfit_id}
                    status = "error"
                    error = "outfit not found"
                    return result
                card = outfit_card(raw_outfit)
                if not card.get("items"):
                    result = {"error": "outfit has no items", "outfit_id": req.outfit_id}
                    status = "error"
                    error = "outfit has no items"
                    return result
                _key, outfit_reason, _item_reasons = _reason_one_outfit(
                    "", card, _reason_mode(),
                )
                result = {
                    "outfit_id": req.outfit_id,
                    "reason": (outfit_reason or "").strip(),
                }
                return result
            except Exception:
                # 内部故障（ES 不可达 / LLM 超时等）不再伪装成 "outfit not found"，
                # re-raise 交给全局 exception_handler 返回诚实 500 + trace_id。
                logger.warning(
                    "[对外接口·regenerate] outfit_id=%s ES 兜底失败，转为 500",
                    req.outfit_id, exc_info=True,
                )
                status = "error"
                error = "internal error"
                raise
        finally:
            self._write_regenerate_audit(
                req, result, status, error, trace_id, app_id, caller, t0,
            )

    def _write_regenerate_audit(
        self,
        req: ExternalRegenerateReasonRequest,
        result: dict[str, Any] | None,
        status: str,
        error: str | None,
        trace_id: str | None,
        app_id: str | None,
        caller: str | None,
        t0: float,
    ) -> None:
        """拼 regenerate_reason 审计文档并写 ES；关闭/失败均静默。"""
        if not self._audit.enabled:
            return
        try:
            input_block = build_input_block(
                outfit_id=req.outfit_id,
                reason_style=req.reason_style,
            )
            meta = {
                "trace_id": trace_id,
                "session_id": None,
                "app_id": app_id,
                "caller": caller,
                "ts": now_iso(),
                "elapsed_ms": int((perf_counter() - t0) * 1000),
                "status": status,
                "error": error,
            }
            doc = build_regenerate_doc(
                input_block=input_block, result=result, meta=meta,
            )
            self._audit.write(doc)
        except Exception:  # noqa: BLE001
            logger.warning("request audit (regenerate) failed", exc_info=True)
