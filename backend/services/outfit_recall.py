"""多路搭配召回：锚定图 + 文本向量拼套 + Query2ES 拼套。"""

from __future__ import annotations

import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

logger = logging.getLogger(__name__)

from backend.api_debug import (
    _debug_recall_io_enabled,
    log_flow,
    log_outfit_rank_scores,
    log_text_search_recall_io,
)
from backend.config import get_elasticsearch_indices, load_config, rank_outfit_limit, recall_outfit_limit
from backend.local_data_store import LocalDataStore
from backend.retrieval.data_facade import DataFacade
from backend.query_keywords import extract_query_keywords
from backend.ranking.outfit_ranker import (
    compute_outfit_rank_breakdown,
    dedupe_outfits_same_skus_prefer_anchor_master,
    llm_rank_outfits,
    rank_outfits,
)
from backend.ranking.partner_ranker import partner_rank_outfits
from backend.ranking.scoring import (
    age_conflict,
    gender_conflict,
    outfit_completeness_score,
    season_conflict,
    tryon_coverage_from_items,
)
from backend.ranking.outfit_conflict import (
    build_attr_milvus_expr,
    build_scene_domain_milvus_expr,
    build_series_milvus_expr,
    check_companion_conflict,
    check_outfit_conflict,
)
from backend.recall_pathway import RecallPathway
from backend.recall_paths_config import es_top_n_per_role, get_outfit_recall_path_switches
from backend.intent.category_l2_pairing import (
    filter_companions_for_target_role,
    merge_milvus_expr,
    resolve_pairing_allowed_companions,
)
from backend.intent.slot_defs import normalize_role
from backend.intent.color_series_mapper import map_color_to_series_list
from backend.intent.color_series_pairing import (
    get_companion_color_series,
    resolve_anchor_color_series,
)
from backend.intent.role_slots import (
    build_modeling_price_milvus_expr,
    build_role_milvus_expr_parts,
    effective_role_budget,
    effective_role_slots,
    role_has_explicit_positive,
    role_negative_slots,
)
from backend.intent.sku_attributes import expand_modeling
from backend.models import UserIntent
from backend.retrieval.es_intent import resolve_es_query_for_role
from backend.retrieval.progressive_relax import (
    get_relax_config,
    run_with_progressive_relax,
)
from backend.retrieval.sku_retriever import SkuRetriever
from backend.services.synthetic_outfit import compose_outfits_from_role_recall, order_outfit_items_by_role
from backend.services.complementary_recall import (
    recall_complementary_composed_outfits,
    recall_complementary_skus,
)
from backend.services.candidate_pool import build_candidate_pool


def _dedupe_role_recall_skus(
    by_role_rows: dict[str, list[dict[str, Any]]],
    anchor_sku_id: str,
    *,
    score_field: str,
) -> dict[str, list[dict[str, Any]]]:
    """各 role 召回 SKU 全局去重后再拼套：同一 sku_id 仅保留分数最高的一条。"""
    aid = (anchor_sku_id or "").strip()
    best: dict[str, tuple[str, float, dict[str, Any]]] = {}
    for role, rows in by_role_rows.items():
        for row in rows:
            sid = str(row.get("sku_id") or "")
            if not sid or sid == aid:
                continue
            sim = float(row.get(score_field) or 0.0)
            prev = best.get(sid)
            if prev is None or sim > prev[1]:
                best[sid] = (role, sim, row)
    out: dict[str, list[dict[str, Any]]] = {}
    for sid, (role, _sim, row) in best.items():
        out.setdefault(role, []).append(row)
    cfg = load_config().get("recommend") or {}
    cat_clr_top_n = int(cfg.get("recall_unique_category_color_top_n") or 0)
    cap = int(cfg.get("default_sku_per_role") or 3)
    for role in list(out):
        rows = out[role]
        rows.sort(
            key=lambda r: float(r.get(score_field) or 0.0),
            reverse=True,
        )
        if cat_clr_top_n > 0:
            seen: dict[tuple[str, str], int] = {}
            filtered: list[dict[str, Any]] = []
            for row in rows:
                cat = str(row.get("category_l2") or "").strip()
                clr = row.get("color_series") or []
                if isinstance(clr, str):
                    clr = [clr] if clr else []
                clr_key = tuple(clr)
                if cat and clr_key:
                    key = (cat, clr_key)
                    cnt = seen.get(key, 0)
                    if cnt >= cat_clr_top_n:
                        continue
                    seen[key] = cnt + 1
                filtered.append(row)
            rows = filtered
        out[role] = rows[:cap]
    return out


def _dedupe_text_recall_skus_by_role(
    by_role_rows: dict[str, list[dict[str, Any]]],
    anchor_sku_id: str,
) -> dict[str, list[dict[str, Any]]]:
    return _dedupe_role_recall_skus(
        by_role_rows,
        anchor_sku_id,
        score_field="_text_vector_sim",
    )


def _lookup_outfits_for_sku(
    store: LocalDataStore | DataFacade,
    sku_id: str,
) -> list[dict[str, Any]]:
    """从固定搭配库查找包含指定 SKU 的搭配（走 DataFacade/ES）。"""
    if hasattr(store, "outfits_by_sku"):
        return store.outfits_by_sku(sku_id)  # type: ignore[attr-defined]
    return []  # 裸 LocalDataStore 已停用本地加载，无数据


def _item_sku_id(item: dict[str, Any]) -> str:
    """从搭配 item 中提取 SKU ID。"""
    raw = (
        item.get("sku_id")
        or item.get("skuId")
        or item.get("attrAlias")
        or item.get("idAlias")
    )
    return str(raw).strip() if raw is not None else ""


def _get_sku_row(
    store: LocalDataStore | DataFacade,
    sku_id: str,
) -> dict[str, Any] | None:
    """从 store 获取 SKU 行数据。"""
    if hasattr(store, "get_sku"):
        return store.get_sku(sku_id)  # type: ignore[attr-defined]
    return None  # 裸 LocalDataStore 已停用本地加载，无数据


def _resolve_item_role(
    item: dict[str, Any],
    store: LocalDataStore | DataFacade,
    row_map: dict[str, dict[str, Any]] | None = None,
) -> str:
    """归一化 item 角色，outfit item.role 缺失时回退 SKU 行 role。

    ES 固定搭配库（micro_guide 源）的 outfit item 可能 role 为空——鞋类既非
    上装也非下装，建库时 upDown 兜底为空。此处用 SKU 行（skus 索引带正确 role）
    兜底，避免覆盖检查误判缺角色而丢弃整套。role 始终为空则返回 ""。
    """
    r = normalize_role(item.get("role"))
    if r:
        return r
    sid = _item_sku_id(item)
    if not sid:
        return ""
    row = None
    if row_map is not None:
        row = row_map.get(sid)
    if row is None:
        row = _get_sku_row(store, sid) or {}
    return normalize_role(row.get("role"))


def _item_color_series(row: dict[str, Any]) -> list[str]:
    """item 的 color_series 列表：优先取字段，缺失则从 color_name/attr_name 派生（与索引建库一致）。"""
    cs = row.get("color_series")
    if isinstance(cs, list):
        vals = [str(x).strip() for x in cs if str(x).strip()]
        if vals:
            return vals
    elif isinstance(cs, str) and cs.strip():
        return [cs.strip()]
    name = str(row.get("color_name") or row.get("attr_name") or "").strip()
    return map_color_to_series_list(name)


def _item_violates_intent(
    row: dict[str, Any],
    intent: UserIntent,
    role: str,
) -> tuple[bool, str]:
    """校验 item 是否符合 intent 的 per-role positive/negative 约束。

    row 需含 color_series/color_name/category_l2/length_class/coverage/scene_domain
    /modeling/price/age（从 SKU 行取，outfit item 本身不带这些字段）。
    返回 (是否违反, 原因)。违反 = 命中 negative 或 不符 positive 显式约束。
    无约束（target_slots 空）时返回 (False, "")。

    覆盖条件与 text_vector / query2es / complementary 三路对齐，不可漏：
    - 全局 age（年龄段冲突，complementary/text_vector 路 Milvus/ES expr 均下推）
    - 全局 ← per-role 覆盖的 modeling（同义词展开：宽松→{宽松,超宽松}）
    - 全局 ← per-role 覆盖的 budget_min/budget_max（price 区间）
    即使无 target_slots，全局 modeling/budget/age 仍生效（外层 gate 已放宽）。
    """
    # 全局 age：任一 role 都校验（与 complementary Milvus expr / text_vector 后过滤一致）。
    # item age 缺失则不据此剔除（交由其它规则）。
    item_age = str(row.get("age") or "").strip()
    if intent.age and item_age and age_conflict(item_age, intent.age):
        return True, f"age={item_age} 与 intent.age={intent.age} 冲突"

    # 版型 + 价格区间：global ← per-role 覆盖（见 role_slots.effective_role_slots /
    # effective_role_budget），与 build_modeling_price_milvus_expr 同源。即使无
    # target_slots，全局 modeling/budget_max 也要过滤——否则"鞋子500以下"会漏过
    # 固定搭配库里的 >500 鞋（bug: anchor_graph 通路曾跳过这两槽位）。
    eff = effective_role_slots(intent, role)
    m_pos = eff.get("modeling")
    if m_pos and m_pos not in ("n/a", ""):
        expanded = expand_modeling(str(m_pos))
        if expanded:
            item_m = str(row.get("modeling") or "").strip()
            # item modeling 缺失（约 30% SKU 为空）不据此剔除，与 Milvus terms
            # 下推不同——此处是固定搭配 item 事后过滤，缺失字段无法判定，放行。
            if item_m and item_m not in expanded:
                return True, f"modeling={item_m} 不在正向 {expanded}"
    bmin, bmax = effective_role_budget(intent, role)
    if bmin or bmax:
        try:
            ip = float(row.get("price") or 0)
        except (TypeError, ValueError):
            ip = 0.0
        if bmin and bmin > 0 and ip < bmin:
            return True, f"price={ip} < budget_min={bmin}"
        if bmax and bmax > 0 and ip > bmax:
            return True, f"price={ip} > budget_max={bmax}"

    if not (intent.target_slots or {}):
        return False, ""
    # negative：target_slots["*"].negative ∪ target_slots[role].negative
    neg = role_negative_slots(intent, role)
    item_cs = _item_color_series(row)
    neg_cs = set(neg.get("color_series") or [])
    if item_cs and any(c in neg_cs for c in item_cs):
        return True, f"color_series={item_cs} 命中否定"
    item_cat = str(row.get("category_l2") or "").strip()
    if item_cat and item_cat in (neg.get("category") or []):
        return True, f"category_l2={item_cat} 命中否定"
    item_lc = str(row.get("length_class") or "").strip()
    if item_lc and item_lc in (neg.get("length_class") or []):
        return True, f"length_class={item_lc} 命中否定"
    item_cov = str(row.get("coverage") or "").strip()
    if item_cov and item_cov in (neg.get("coverage") or []):
        return True, f"coverage={item_cov} 命中否定"
    item_sd = str(row.get("scene_domain") or "").strip()
    if item_sd and item_sd in (neg.get("scene_domain") or []):
        return True, f"scene_domain={item_sd} 命中否定"
    # 版型否定：多值各自同义词展开后并集（修身→{修身,紧身}），命中即违反。
    # 与 build_role_milvus_expr_parts / build_role_es_must_not 的 modeling 否定同源。
    m_neg = neg.get("modeling")
    if m_neg:
        item_m = str(row.get("modeling") or "").strip()
        if item_m:
            neg_union: list[str] = []
            seen_m: set[str] = set()
            for v in m_neg:
                for e in expand_modeling(str(v)):
                    if e not in seen_m:
                        seen_m.add(e)
                        neg_union.append(e)
            if neg_union and item_m in neg_union:
                return True, f"modeling={item_m} 命中否定 {neg_union}"

    # positive：target_slots[role].positive（仅 per-role，不含全局 flat）
    pos = ((intent.target_slots or {}).get(role) or {}).get("positive") or {}
    if not pos:
        return False, ""
    pos_cs = pos.get("color_series")
    if pos_cs and item_cs and not any(c in pos_cs for c in item_cs):
        return True, f"color_series={item_cs} 不在正向 {pos_cs}"
    if pos.get("category") and item_cat and item_cat not in pos["category"]:
        return True, f"category_l2={item_cat} 不在正向 {pos['category']}"
    if pos.get("length_class") and item_lc and item_lc not in pos["length_class"]:
        return True, f"length_class={item_lc} 不在正向 {pos['length_class']}"
    if pos.get("coverage") and item_cov and item_cov not in pos["coverage"]:
        return True, f"coverage={item_cov} 不在正向 {pos['coverage']}"
    if pos.get("scene_domain") and item_sd and item_sd not in pos["scene_domain"]:
        return True, f"scene_domain={item_sd} 不在正向 {pos['scene_domain']}"
    # per-role series 正向：固定搭配库通路同样尊重——bypass 跳过锚点同系隔离 +
    # _series_conflict 安全网让路后，per-role positive.series 是唯一兜底，漏读
    # 会让锚点（如 HERITAGE）原配的非目标系列裤子长驱直入（与 modeling 同源 bug）。
    # item series 缺失不据此剔除（交其它规则），与 modeling 缺失放行一致。
    pos_series = pos.get("series")
    if pos_series:
        item_series = str(row.get("series") or "").strip()
        if item_series and item_series != str(pos_series).strip():
            return True, f"series={item_series} 不在正向 {pos_series}"
    # per-role modeling positive 已在上方用 effective（global←per-role）覆盖处理；
    # per-role budget_min/max 同理。此处不重复，避免双重判定。
    return False, ""


def recall_anchor_graph_outfits(
    store: LocalDataStore | DataFacade,
    anchor_sku_id: str,
    *,
    fallback_anchor_skus: list[str] | None = None,
    candidate_skus: list[str] | None = None,
    intent_gender: str | None = None,
    intent_season: list[str] | None = None,
    intent_target_roles: list[str] | None = None,
    anchor_attrs: dict[str, Any] | None = None,
    intent: UserIntent | None = None,
) -> tuple[list[dict[str, Any]], set[str]]:
    """通路1：相似固定搭配召回：图向量近邻 SKU → 批量查固定搭配库。

    candidate_skus 优先：将全部 Milvus 召回 SKU（含 anchor）合并为一次
    ES terms 查询，一次网络往返拿到所有搭配。不再逐个 fallback。

    搭配中若包含近邻 SKU（非 anchor），自动替换为 anchor 单品数据。

    当 candidate_skus 未提供时，兼容旧逻辑：仅用 anchor_sku_id 查询。

    intent_target_roles：意图解析给出的互补角色（中文，如 ["鞋","配饰"]）。
    非空时，搭配中非 anchor 且角色不在该集合内的单品会被**剪枝**（移除该单品，
    保留其余搭配），而非剔除整套——例如连衣裙 anchor 的 target_roles 为
    [鞋, 配饰]，含上装/下装的固定搭配只移除上装/下装，保留连衣裙+配饰。
    角色缺失（空）的单品不剪枝，交由冲突规则兜底。剪枝后不足 2 件则放弃。
    此外要求剪枝后的搭配**覆盖所有 target_roles**：缺任一角色则丢弃整套。
    """
    aid = (anchor_sku_id or "").strip()
    if not aid and not candidate_skus:
        logger.info("[anchor_graph] anchor_sku_id 为空且无 candidate_skus，跳过")
        return [], set()

    # 确定要批量查询的 SKU 列表
    if candidate_skus:
        # 去重并保持顺序（anchor 优先）
        seen: set[str] = set()
        ordered: list[str] = []
        if aid:
            ordered.append(aid)
            seen.add(aid)
        for sid in candidate_skus:
            sid = (sid or "").strip()
            if sid and sid not in seen:
                seen.add(sid)
                ordered.append(sid)
        query_skus = ordered
    else:
        query_skus = [aid]

    # 近邻 SKU 集合（排除 anchor），用于后续替换
    neighbor_skus = {s for s in query_skus if s != aid}

    # 批量查固定搭配库
    raw_outfits: list[dict[str, Any]] = []
    if hasattr(store, "outfits_by_skus_batch"):
        raw_outfits = store.outfits_by_skus_batch(query_skus)  # type: ignore[attr-defined]
    else:
        # fallback: 逐个查（本地 store 路径）
        seen_oid: set[str] = set()
        for sid in query_skus:
            for o in _lookup_outfits_for_sku(store, sid):
                oid = str(o.get("outfit_id") or o.get("idMatch") or "")
                if oid and oid not in seen_oid:
                    seen_oid.add(oid)
                    raw_outfits.append(o)

    logger.info(
        "[anchor_graph] 批量查询：query_skus=%d, outfits=%d, skus=%s",
        len(query_skus), len(raw_outfits),
        [s for s in query_skus[:5]],
    )

    # 获取 anchor 单品数据，供替换和 anchor-first 补全用
    anchor_row: dict[str, Any] | None = None
    if aid:
        anchor_row = _get_sku_row(store, aid)

    # target_roles 允许的互补角色集合（归一化为英文 token）
    allowed_target_roles: set[str] | None = None
    if intent_target_roles:
        allowed_target_roles = {
            normalize_role(r) for r in intent_target_roles if str(r).strip()
        }
        if not allowed_target_roles:
            allowed_target_roles = None

    outfits: list[dict[str, Any]] = []
    src_ids: set[str] = set()
    replace_count = 0
    gender_skip_count = 0
    season_skip_count = 0
    target_role_skip_count = 0
    for raw in raw_outfits:
        if not raw:
            continue
        o = dict(raw)
        oid = str(o.get("outfit_id") or o.get("idMatch") or "")
        o["source"] = "anchor_graph"
        o["_recall_path"] = RecallPathway.OUTFIT_ANCHOR_GRAPH.value

        # 替换搭配中的近邻 SKU 单品为 anchor。
        # 注意：若搭配**已包含 anchor 本身**（anchor 自身命中库内搭配），
        # 则不再做近邻→anchor 替换——否则会复制出第二件 sku_id==anchor 的
        # 单品，anchor-first 阶段又会把两件都标 is_anchor=True，target_role
        # 剪枝无法去除，最终一套 synth_graph 里同货号出现两次。此时 anchor
        # 已是 master，多余的近邻交由 target_role / 冲突规则处理即可。
        replaced_in_outfit = False
        anchor_in_original = aid and any(
            isinstance(it, dict) and _item_sku_id(it) == aid
            for it in (o.get("items") or [])
        )
        if anchor_row and neighbor_skus and not anchor_in_original:
            items = o.get("items") or []
            new_items = []
            for item in items:
                if not isinstance(item, dict):
                    new_items.append(item)
                    continue
                item_sid = _item_sku_id(item)
                if item_sid and item_sid in neighbor_skus and not replaced_in_outfit:
                    # 用 anchor 数据替换该单品
                    replaced_item = dict(item)
                    replaced_item["sku_id"] = aid
                    replaced_item["attrAlias"] = aid
                    replaced_item["idAlias"] = aid
                    replaced_item["isMaster"] = True
                    replaced_item["is_master"] = True
                    replaced_item["is_anchor"] = True
                    for key in ("title", "price", "role", "category_l2",
                                "display_image", "tryon_image", "spu_id",
                                "series", "color", "attributes", "images"):
                        val = anchor_row.get(key)
                        if val is not None:
                            replaced_item[key] = val
                    logger.info(
                        "[anchor_graph] 替换 item: outfit=%s, "
                        "原sku=%s → anchor=%s, spu_id=%s",
                        oid, item_sid, aid,
                        replaced_item.get("spu_id"),
                    )
                    new_items.append(replaced_item)
                    replaced_in_outfit = True
                    replace_count += 1
                else:
                    new_items.append(item)
            o["items"] = new_items
            # 同步更新 outfit 级 sku_ids
            if replaced_in_outfit and "sku_ids" in o:
                o["sku_ids"] = [
                    aid if s in neighbor_skus else s
                    for s in o["sku_ids"]
                ]

        # 判断 anchor 是否已在搭配中（未经近邻替换，而是 anchor 自身命中）
        anchor_already_in = False
        if not replaced_in_outfit and aid:
            anchor_already_in = any(
                isinstance(it, dict) and _item_sku_id(it) == aid
                for it in (o.get("items") or [])
            )

        # target_role 剪枝：移除非 anchor 且角色不在 target_roles 内的单品，
        # 保留其余搭配（不剔除整套）。例：连衣裙 anchor 的 target_roles 为
        # [鞋, 配饰]，含上装/下装的固定搭配只移除上装/下装，保留连衣裙+配饰。
        # 角色缺失（空）的单品不剪枝——无法判定，交由冲突规则兜底。
        if allowed_target_roles is not None:
            items_before = o.get("items") or []
            pruned: list[dict[str, Any]] = []
            dropped_roles: list[str] = []
            for item in items_before:
                if not isinstance(item, dict):
                    pruned.append(item)
                    continue
                item_sid = _item_sku_id(item)
                is_anchor_item = (
                    (aid and item_sid == aid)
                    or item.get("is_anchor")
                    or item.get("is_master")
                )
                if is_anchor_item:
                    pruned.append(item)
                    continue
                item_role = normalize_role(item.get("role"))
                if item_role and item_role not in allowed_target_roles:
                    dropped_roles.append(item_role)
                    continue
                pruned.append(item)
            if dropped_roles:
                logger.info(
                    "[anchor_graph·target_role剪枝] outfit=%s 移除 target_roles=%s "
                    "外的单品（角色 %s），%d件→%d件",
                    oid, sorted(allowed_target_roles), sorted(set(dropped_roles)),
                    len(items_before), len(pruned),
                )
                o["items"] = pruned
                if "sku_ids" in o:
                    keep_sids = {
                        _item_sku_id(it) for it in pruned if isinstance(it, dict)
                    }
                    o["sku_ids"] = [s for s in o["sku_ids"] if s in keep_sids]
            # 剪枝后仅剩 anchor（不足 2 件）则该搭配无可搭配项，放弃
            remaining = [
                it for it in (o.get("items") or [])
                if isinstance(it, dict) and _item_sku_id(it)
            ]
            if len(remaining) < 2:
                logger.info(
                    "[anchor_graph·target_role剪枝] outfit=%s 剪枝后仅%d件，跳过",
                    oid, len(remaining),
                )
                target_role_skip_count += 1
                continue

            # target_role 覆盖检查：剪枝后的搭配必须覆盖**所有** target_roles，
            # 缺任一角色则丢弃整套（而非保留缺项的部分搭配）。
            # item.role 缺失时回退 SKU 行 role（鞋类 outfit item role 常为空）。
            present_target_roles: set[str] = set()
            for it in (o.get("items") or []):
                if not isinstance(it, dict):
                    continue
                if _item_sku_id(it) == aid:
                    continue
                if it.get("is_anchor") or it.get("is_master"):
                    continue
                r = _resolve_item_role(it, store)
                if r:
                    present_target_roles.add(r)
            missing_target_roles = allowed_target_roles - present_target_roles
            if missing_target_roles:
                logger.info(
                    "[anchor_graph·target_role覆盖] outfit=%s 缺 target_roles=%s "
                    "（已有=%s，要求=%s），跳过整套",
                    oid, sorted(missing_target_roles),
                    sorted(present_target_roles), sorted(allowed_target_roles),
                )
                target_role_skip_count += 1
                continue

        # intent 符合检测：非 anchor item 不符 per-role positive/negative（color_series/
        # category/length_class/coverage/scene_domain/modeling）或全局 modeling/budget/age
        # 则剔除。outfit item 不带这些字段，按 sku_id 取 SKU 行（color_series 缺失则从
        # color_name 派生，与建库一致）。
        # gate 放宽：全局 modeling/budget_min/budget_max/age 即使无 target_slots 也要过滤
        # （否则"鞋子500以下"会漏过固定搭配库里的 >500 鞋）。
        if intent is not None and (
            intent.target_slots
            or intent.modeling
            or intent.budget_min
            or intent.budget_max
            or intent.age
        ):
            items_before = o.get("items") or []
            need_sids = [
                s for s in (_item_sku_id(it) for it in items_before
                            if isinstance(it, dict))
                if s and s != aid
            ]
            row_map: dict[str, dict[str, Any]] = {}
            if need_sids and hasattr(store, "get_skus"):
                try:
                    rows = store.get_skus(need_sids)  # type: ignore[attr-defined]
                    row_map = {
                        r.get("sku_id"): r for r in (rows or [])
                        if isinstance(r, dict) and r.get("sku_id")
                    }
                except Exception:  # noqa: BLE001
                    row_map = {}
            pruned2: list[dict[str, Any]] = []
            dropped_info: list[str] = []
            for it in items_before:
                if not isinstance(it, dict):
                    pruned2.append(it)
                    continue
                sid = _item_sku_id(it)
                if sid == aid or it.get("is_anchor") or it.get("is_master"):
                    pruned2.append(it)
                    continue
                row = row_map.get(sid) or _get_sku_row(store, sid) or {}
                role = normalize_role(it.get("role") or row.get("role"))
                if not role:
                    pruned2.append(it)
                    continue
                violated, reason = _item_violates_intent(row, intent, role)
                if violated:
                    dropped_info.append(f"{sid}({role}): {reason}")
                    continue
                pruned2.append(it)
            if dropped_info:
                logger.info(
                    "[anchor_graph·符合检测] outfit=%s 剔除非符合单品: %s",
                    oid, "; ".join(dropped_info),
                )
                o["items"] = pruned2
                if "sku_ids" in o:
                    keep_sids = {
                        _item_sku_id(it) for it in pruned2 if isinstance(it, dict)
                    }
                    o["sku_ids"] = [s for s in o["sku_ids"] if s in keep_sids]
                # 剪枝后覆盖检查：缺任一 target_role 则丢弃整套
                if allowed_target_roles is not None:
                    present = set()
                    for it in (o.get("items") or []):
                        if not isinstance(it, dict):
                            continue
                        if _item_sku_id(it) == aid or it.get("is_anchor") or it.get("is_master"):
                            continue
                        r = _resolve_item_role(it, store, row_map)
                        if r:
                            present.add(r)
                    missing = allowed_target_roles - present
                    if missing:
                        logger.info(
                            "[anchor_graph·符合检测] outfit=%s 剪枝后缺 target_roles=%s，跳过",
                            oid, sorted(missing),
                        )
                        target_role_skip_count += 1
                        continue

        # anchor 单品标记为 master（供前端高亮与去重/排序），展示位置由
        # order_outfit_items_by_role 按 role 决定（不再强制 anchor 首位）。
        # 重新生成 outfit_id 防止覆盖原数据。
        need_anchor_first = replaced_in_outfit or anchor_already_in
        if need_anchor_first:
            items = o.get("items") or []
            # 清除其他 item 上残留的 master 标记，仅保留 anchor
            for it in items:
                if isinstance(it, dict) and _item_sku_id(it) != aid:
                    it["isMaster"] = False
                    it["is_master"] = False
            # 确保 anchor item 标记为 master
            for it in items:
                if isinstance(it, dict) and _item_sku_id(it) == aid:
                    it["isMaster"] = True
                    it["is_master"] = True
                    it["is_anchor"] = True
                    # anchor 自身命中时用 anchor_row 补全数据
                    if anchor_already_in and not replaced_in_outfit and anchor_row:
                        for key in ("title", "price", "role", "category_l2",
                                    "display_image", "tryon_image", "spu_id",
                                    "series", "color", "attributes", "images"):
                            val = anchor_row.get(key)
                            if val is not None:
                                it[key] = val
                    break
            o["items"] = items
            sku_part = "_".join(
                sorted(str(_item_sku_id(it)) for it in items if isinstance(it, dict))[:6],
            )
            raw_id = f"synth_graph_{sku_part}"
            oid = f"synth_graph_{hashlib.md5(raw_id.encode()).hexdigest()[:8]}"
            o["outfit_id"] = oid
            o["master_sku_id"] = aid
            o["master_spu_id"] = (anchor_row.get("spu_id") if anchor_row else None)
            anchor_title = str(anchor_row.get("title") or aid) if anchor_row else aid
            o["name"] = f"相似固定搭配 · {anchor_title} 等{len(items)}件"
            logger.info(
                "[anchor_graph] 重新生成 outfit_id=%s, master_sku=%s, "
                "原outfit_id=%s, anchor_already_in=%s",
                oid, aid, str(raw.get("outfit_id") or raw.get("idMatch") or ""),
                anchor_already_in,
            )

        # 规范化：确保每个 item 都有 sku_id 字段（ES 原始数据可能只有 attrAlias）
        normalized_items = []
        for item in (o.get("items") or []):
            if not isinstance(item, dict):
                normalized_items.append(item)
                continue
            sid = _item_sku_id(item)
            if sid and not item.get("sku_id"):
                item = dict(item)
                item["sku_id"] = sid
            normalized_items.append(item)
        o["items"] = normalized_items

        # gender 过滤：搭配中任一非 anchor 单品 gender 与意图冲突则跳过
        if intent_gender:
            outfit_gender = o.get("gender")
            if outfit_gender and gender_conflict(outfit_gender, intent_gender):
                logger.info(
                    "[anchor_graph·gender过滤] outfit=%s outfit_gender=%s "
                    "与 intent_gender=%s 冲突，跳过",
                    oid, outfit_gender, intent_gender,
                )
                gender_skip_count += 1
                continue
            skip = False
            for item in o.get("items") or []:
                if not isinstance(item, dict):
                    continue
                item_sid = _item_sku_id(item)
                if item_sid == aid:
                    continue
                item_gender = (item.get("gender")
                               or (item.get("attributes") or {}).get("sex"))
                if item_gender and gender_conflict(item_gender, intent_gender):
                    logger.info(
                        "[anchor_graph·gender过滤] outfit=%s item_sku=%s "
                        "item_gender=%s 与 intent_gender=%s 冲突，跳过整套",
                        oid, item_sid, item_gender, intent_gender,
                    )
                    skip = True
                    break
            if skip:
                gender_skip_count += 1
                continue

        # season 过滤：搭配中任一非 anchor 单品 season 与意图无交集则跳过
        if intent_season:
            skip_season = False
            for item in o.get("items") or []:
                if not isinstance(item, dict):
                    continue
                item_sid = _item_sku_id(item)
                if item_sid == aid:
                    continue
                item_season = item.get("season")
                if item_season and season_conflict(item_season, intent_season):
                    logger.info(
                        "[anchor_graph·season过滤] outfit=%s item_sku=%s "
                        "item_season=%s 与 intent_season=%s 无交集，跳过整套",
                        oid, item_sid, item_season, intent_season,
                    )
                    skip_season = True
                    break
            if skip_season:
                season_skip_count += 1
                continue

        # 统一冲突检测：基于结构化属性，跳过含冲突单品的整套。
        # 冲突信号优先取意图模块融合产出的 anchor_attrs（上传图场景下不依赖
        # 0.61 模糊匹配的 SKU 行），其次回退 anchor_row（真实 SKU 行）。
        # 用户 per-role 显式意图下传：固定搭配库同样尊重——有任一 positive 的 role，
        # 其所有锚点驱动冲突规则一律让路（如「网球裤」daily 锚点不再拒 tennis 下装、
        # 「白色短裤」长袖锚点不再拒短款、「白色裤子」daily 锚点不再拒 golf 长裤）。
        conflict_anchor = anchor_attrs or anchor_row
        _bypass_roles: set[str] = set()
        if intent is not None:
            for _r in (intent.target_roles or []):
                if role_has_explicit_positive(intent, _r):
                    _bypass_roles.add(_r)
        if conflict_anchor and check_outfit_conflict(
            conflict_anchor, o.get("items") or [], anchor_id=aid,
            role_bypass_all=_bypass_roles or None,
        ):
            logger.info(
                "[anchor_graph·冲突过滤] outfit=%s 含与锚点属性冲突的单品，跳过整套",
                oid,
            )
            continue

        # 补全 anchor_graph 搭配缺失的排序字段
        items = o.get("items") or []
        roles_set: set[str] = set()
        search_texts: list[str] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            role = str(it.get("role") or "").strip()
            if role:
                roles_set.add(role)
            st = str(it.get("search_text") or "")
            if st:
                search_texts.append(st)
        if "outfit_completeness_score" not in o:
            o["outfit_completeness_score"] = outfit_completeness_score(roles_set)
        if "tryon_coverage" not in o:
            o["tryon_coverage"] = tryon_coverage_from_items(items)
        if "search_text" not in o and search_texts:
            o["search_text"] = " ".join(search_texts)

        # 排序前补全空 role：ES 固定搭配库（micro_guide 源）的 item.role
        # 可能为空——鞋类既非上装也非下装，建库时 upDown 兜底为空。若不
        # 补全，order_outfit_items_by_role 只读 it.get("role")，空 role 经
        # role_display_priority 返回 99 垫底，鞋会被错排到配饰之后
        # （top→bottoms→accessory→shoes）。此处回退查 SKU 行补全 role。
        # roles_set（完整度分）已在上方用原 role 计算，此处不重算，避免
        # 影响整套餐的完整度分与排序权重。
        for it in items:
            if isinstance(it, dict) and not normalize_role(it.get("role")):
                resolved = _resolve_item_role(it, store)
                if resolved:
                    it["role"] = resolved

        # 搭配内单品排序：anchor 首位，其余按 role 优先级
        # top > bottoms/dress > shoes > accessory
        o["items"] = order_outfit_items_by_role(o.get("items") or [], aid)

        outfits.append(o)
        if oid:
            src_ids.add(oid)

    logger.info(
        "[anchor_graph] 最终结果：anchor=%s, query_skus=%d, outfits=%d, "
        "outfit_ids=%s, neighbor_replaced=%d, gender_skipped=%d, "
        "season_skipped=%d, target_role_skipped=%d",
        aid, len(query_skus), len(outfits),
        list(src_ids)[:10], replace_count, gender_skip_count,
        season_skip_count, target_role_skip_count,
    )
    return outfits, src_ids


def recall_text_vector_skus(
    sku_r: SkuRetriever,
    intent: UserIntent,
    compose_anchor_row: dict[str, Any] | None,
    *,
    trace_id: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """通路2·召回阶段：意图关键词按 role 文本向量召回 SKU（去重）。

    返回 ``{role: [sku_row, ...]}``，每行带 ``_text_vector_sim``。
    组合阶段交给 ``compose_outfits_from_role_recall``（per_channel 模式）或
    全局候选池（global 模式）。

    无 ``compose_anchor_row`` 时仍走文本向量召回，各 target_role 的 SKU 笛卡尔积组合。
    有 ``compose_anchor_row`` 时，图搜 anchor SKU + 各 target_role SKU 笛卡尔积组合。
    """
    keywords = extract_query_keywords(intent, trace_id=trace_id)
    roles = [str(r).strip() for r in (intent.target_roles or []) if str(r).strip()]
    if not keywords and not roles:
        return {}

    cfg = load_config()
    rec = cfg.get("recommend") or {}
    raw_by_role: dict[str, list[dict[str, Any]]] = {}
    gender = intent.gender
    age = intent.age
    season = list(intent.season or [])
    budget = intent.budget_max
    compose_anchor_id = str((compose_anchor_row or {}).get("sku_id") or "")
    anchor_role = str((compose_anchor_row or {}).get("role") or intent.anchor_role or "")
    enable_cat2 = bool(rec.get("enable_category_l2_pairing", True))
    enable_cs = bool(rec.get("enable_color_series_pairing", True))
    mv = cfg.get("milvus") or {}
    milvus_scalar_expr = bool(mv.get("text_vector_expr_filter", True))
    allowed_cat2 = resolve_pairing_allowed_companions(
        compose_anchor_row,
        intent_categories=list(intent.category or []),
        intent_override_categories=list(intent.category or []),
    ) if enable_cat2 else None
    # 色系过滤：intent 色系（无方向，对称规则）或锚点色系（按 anchor→companion 方向
    # 取 fila_sku 方向化 YAML：上装→下装 与 下装→上装 不对称）。
    user_cs = list(intent.color_series or []) if enable_cs else []
    anchor_cs = resolve_anchor_color_series(compose_anchor_row) if enable_cs else None

    for i, role in enumerate(roles):
        if i >= len(keywords):
            break
        kw = keywords[i]
        from backend.intent.role_slots import (
            build_modeling_price_milvus_expr,
            build_role_milvus_expr_parts,
            per_role_color_series,
        )
        _bypass = role_has_explicit_positive(intent, role)
        # pairing 让路：用户对该 role 有显式 positive 时，中类互补白名单与色系配对
        # 都不回退——类型正确性由 role== 守卫保证，色系/品类约束交给 per-role 正向过滤
        # （build_role_milvus_expr_parts）。
        role_companions = None if _bypass else filter_companions_for_target_role(allowed_cat2, role)
        per_role_cs = per_role_color_series(intent, role)
        if per_role_cs:
            cs_filter = per_role_cs
            logger.info(
                "[text_vector·cs_override] role=%s 用户显式 color_series=%s，覆盖 pairing cs_filter",
                role, per_role_cs,
            )
        elif not enable_cs or _bypass:
            cs_filter = None
        elif user_cs:
            cs_filter = get_companion_color_series(user_cs[0]) or user_cs
        else:
            cs_filter = (
                get_companion_color_series(
                    anchor_cs,
                    anchor_role=anchor_role,
                    companion_role=role,
                ) if anchor_cs else None
            )
        if _bypass:
            logger.info(
                "[text_vector·explicit_bypass] role=%s 用户有显式 positive，跳过锚点场景/结构/系列/pairing 隔离",
                role,
            )
        # scene/series/attr/modeling+price 各片段由 _rebuild_text_attr_expr 在
        # progressive relax 每轮按 dropped 重建，不再在此一次性拼装贯穿。
        recall_mode = str(rec.get("text_recall_mode") or "dense").lower()
        relax_enabled, relax_priority, relax_min_hits = get_relax_config()

        def _search_fn(dropped: set[str]) -> list[tuple[str, float, float]]:
            """progressive relax 单轮：按 dropped 重建 attr_expr + 放宽 pairing/up_time。"""
            _attr = _rebuild_text_attr_expr(
                intent, role, compose_anchor_row, _bypass, dropped,
            )
            _cat2 = None if "category_l2" in dropped else role_companions
            _cs = None if "color_series" in dropped else cs_filter
            _skip_up = "up_time" in dropped
            common = dict(
                role_filter=role, gender_filter=gender, age_filter=age,
                category_l2_filter=_cat2, color_series_filter=_cs,
                trace_id=trace_id, attr_expr=_attr,
            )
            if recall_mode == "hybrid":
                # hybrid leg；0 命中再走 dense leg（保留旧 hybrid→dense fallback 行为）
                hits = sku_r.recall_by_hybrid([kw], skip_up_time=_skip_up, **common)
                if not hits:
                    hits = sku_r.recall_by_text_vector_keywords(
                        [kw], skip_up_time=_skip_up, **common,
                    )
                return hits
            return sku_r.recall_by_text_vector_keywords([kw], skip_up_time=_skip_up, **common)

        if relax_enabled:
            pairs, dropped_list = run_with_progressive_relax(
                _search_fn, relax_priority, relax_min_hits,
            )
            if dropped_list:
                logger.info(
                    "[text_recall·progressive_relax] role=%s kw=%s dropped=%s pairs=%d",
                    role, kw, dropped_list, len(pairs),
                )
        else:
            pairs = _search_fn(set())
            dropped_list = []

        # B: 低召回数时降阈值二次召回（保留 cs_filter/category 等所有过滤）。
        # progressive relax 处理 0 命中；此处处理 1-2 条的相似度阈值边缘补充。
        # hybrid 路不做（RRF 量纲，无 min_sim 阈值）。
        _LOW_RECALL_THRESHOLD = 3
        if recall_mode != "hybrid" and len(pairs) < _LOW_RECALL_THRESHOLD:
            cfg_min = float(rec.get("sku_text_vector_min_similarity") or 0.0)
            retry_min = max(0.0, cfg_min - 0.15)
            if retry_min < cfg_min:
                logger.info(
                    "[text_vector·低召回降阈值] role=%s 召回%d条<%d，降阈值 %.2f→%.2f 二次召回",
                    role, len(pairs), _LOW_RECALL_THRESHOLD, cfg_min, retry_min,
                )
                _attr0 = _rebuild_text_attr_expr(
                    intent, role, compose_anchor_row, _bypass, set(),
                )
                pairs2 = sku_r.recall_by_text_vector_keywords(
                    [kw],
                    role_filter=role, gender_filter=gender, age_filter=age,
                    category_l2_filter=role_companions, color_series_filter=cs_filter,
                    trace_id=trace_id, attr_expr=_attr0,
                    min_similarity_override=retry_min,
                )
                seen_sim: dict[str, float] = {sid: sim for sid, sim, _ in pairs}
                for sid, sim, _raw in pairs2:
                    if sid not in seen_sim or sim > seen_sim[sid]:
                        seen_sim[sid] = sim
                        pairs.append((sid, sim, _raw))
                pairs.sort(key=lambda x: x[1], reverse=True)
        picked: list[dict[str, Any]] = []
        seen_in_role: set[str] = set()
        for sid, sim, _raw in pairs:
            if sid in seen_in_role:
                continue
            row = sku_r.get_sku(sid)
            if not row:
                continue
            if not milvus_scalar_expr:
                if str(row.get("role") or "") != role:
                    continue
                if gender and gender_conflict(row.get("gender"), gender):
                    continue
                if age and age_conflict(row.get("age"), age):
                    continue
            if season and season_conflict(row.get("season"), season):
                continue
            # 安全网精排：expr 粗排已下推单侧属性规则，此处兜底成对冲突规则
            if compose_anchor_row and check_companion_conflict(
                compose_anchor_row, row,
                bypass_all=role_has_explicit_positive(intent, role),
            ):
                logger.info(
                    "[text_vector·冲突过滤] anchor=%s 与 sku=%s "
                    "category_l2=%s title=%s 属性冲突，跳过",
                    compose_anchor_id, sid, row.get("category_l2"), row.get("title"),
                )
                continue
            seen_in_role.add(sid)
            if compose_anchor_id and sid == compose_anchor_id:
                continue
            c = dict(row)
            c["_text_vector_sim"] = float(sim)
            picked.append(c)
        if picked:
            raw_by_role[role] = picked

    raw_sku_n = sum(len(v) for v in raw_by_role.values())
    by_role_rows = _dedupe_text_recall_skus_by_role(
        raw_by_role,
        compose_anchor_id,
    )
    deduped_sku_n = sum(len(v) for v in by_role_rows.values())

    if _debug_recall_io_enabled():
        log_flow(
            "text_vector_sku_dedupe",
            {
                "trace_id": trace_id,
                "compose_anchor_sku_id": compose_anchor_id,
                "sku_before_dedupe": raw_sku_n,
                "sku_after_dedupe": deduped_sku_n,
                "roles": {
                    r: [x.get("sku_id") for x in rows]
                    for r, rows in by_role_rows.items()
                },
            },
        )
    return by_role_rows


def recall_text_vector_composed_outfits(
    sku_r: SkuRetriever,
    intent: UserIntent,
    compose_anchor_row: dict[str, Any] | None,
    *,
    trace_id: str | None = None,
) -> list[dict[str, Any]]:
    """通路2·per_channel 模式：文本向量召回 SKU（去重）→ 直接拼套。

    global 模式下不调用本函数，改由 multi_path_recall 聚合进全局池统一拼套。
    """
    cfg = load_config()
    rec = cfg.get("recommend") or {}
    per_role = int(rec.get("default_sku_per_role") or 3)
    max_outfits = recall_outfit_limit(cfg)

    by_role_rows = recall_text_vector_skus(
        sku_r, intent, compose_anchor_row, trace_id=trace_id,
    )

    anchor_for_compose: dict[str, Any] | None = None
    if compose_anchor_row:
        anchor_for_compose = dict(compose_anchor_row)
        anchor_for_compose["_is_image_input_anchor"] = True

    composed = compose_outfits_from_role_recall(
        anchor_for_compose,
        by_role_rows,
        max_outfits=max_outfits,
        picks_per_role=per_role,
        source="text_vector_compose",
    )
    for o in composed:
        o["source"] = "text_vector_compose"
        o["_recall_path"] = RecallPathway.OUTFIT_TEXT_VECTOR_COMPOSE.value
    return composed


# progressive relax 丢弃 slot 名 → resolve_es_query_for_role 的 skip-knob 参数。
# 硬约束（gender/season/age）不在其中，循环不会触及。
_ES_SKIP_FLAG = {
    "modeling": "skip_modeling",
    "length_class": "skip_length_class",
    "coverage": "skip_coverage",
    "series": "skip_series",
    "scene_domain": "skip_scene_domain",
    "category_l2": "skip_category_l2",
    "anchor_attr_must_not": "skip_anchor_attr_must_not",
    "up_time": "skip_up_time",
    "price": "skip_price",
}


def _es_relax_kwargs(
    dropped: set[str],
    *,
    allowed_cat2: list[str] | None,
    allowed_cs_role: list[str] | None,
    enable_cs: bool,
) -> dict[str, Any]:
    """将 dropped 的 slot 名映射为 resolve_es_query_for_role 的参数。"""
    kw: dict[str, Any] = {
        "allowed_companion_cat2": None if "category_l2" in dropped else allowed_cat2,
        "allowed_companion_color_series": None if "color_series" in dropped else allowed_cs_role,
        "skip_color_series": ("color_series" in dropped) or (not enable_cs),
    }
    for slot, flag in _ES_SKIP_FLAG.items():
        if slot in dropped:
            kw[flag] = True
    return kw


def _rebuild_text_attr_expr(
    intent: UserIntent,
    role: str,
    compose_anchor_row: dict[str, Any] | None,
    _bypass: bool,
    dropped: set[str],
) -> str | None:
    """按 dropped 集合重建 text/hybrid 路的 attr_expr。

    落地 progressive relax：每轮按丢弃集重新拼装 scene/series/attr/per-role/modeling+price，
    而非用首次拼好的 attr_expr 贯穿——这样 series/scene_domain/anchor_attr_must_not/
    modeling/price/length_class/coverage 可被独立放宽。
    """
    parts: list[str | None] = []
    if "anchor_attr_must_not" not in dropped:
        parts.append(build_attr_milvus_expr(compose_anchor_row, role, bypass_all=_bypass))
    if not _bypass and "scene_domain" not in dropped:
        parts.append(build_scene_domain_milvus_expr(compose_anchor_row, role))
    if not _bypass and "series" not in dropped:
        parts.append(build_series_milvus_expr(compose_anchor_row, role, intent.series or ""))
    # per-role 正向/否定：length_class/coverage/modeling/color_series/category/series/scene_domain
    per_role_skip = {
        "length_class", "coverage", "modeling", "color_series",
        "category", "series", "scene_domain",
    } & dropped
    parts.extend(build_role_milvus_expr_parts(
        intent, role, include_global=False,
        skip_slots=per_role_skip or None,
    ))
    # 版型 + 价格（链尾放宽）
    mp = build_modeling_price_milvus_expr(
        intent, role,
        skip_modeling=("modeling" in dropped),
        skip_price=("price" in dropped),
    )
    if mp:
        parts.append(mp)
    return merge_milvus_expr(*parts)


def recall_query2es_skus(
    sku_r: SkuRetriever,
    intent: UserIntent,
    compose_anchor_row: dict[str, Any] | None,
    *,
    image_base64: str | None = None,
    trace_id: str | None = None,
    model_override: str | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """通路3·召回阶段：按 target_role 用规则/LLM 生成 ES query → 检索 top N → 去重。

    返回 ``(by_role_rows, es_debug)``。每行带 ``_es_score``。
    es_debug 包含各 role 的 ES query 与 meta。
    """
    if not sku_r._es.available:  # noqa: SLF001
        if _debug_recall_io_enabled():
            log_flow(
                "query2es_compose_skipped",
                {
                    "trace_id": trace_id,
                    "reason": "es_unavailable",
                },
            )
        return {}, {}

    roles = [str(r).strip() for r in (intent.target_roles or []) if str(r).strip()]
    if not roles:
        return {}, {}

    cfg = load_config()
    rec = cfg.get("recommend") or {}
    index_name = get_elasticsearch_indices(cfg)["skus"]
    per_role = es_top_n_per_role()
    llm_on = bool(rec.get("query2es_llm_enabled", True))
    gender = intent.gender
    season = list(intent.season or [])
    budget = intent.budget_max
    compose_anchor_id = str((compose_anchor_row or {}).get("sku_id") or "")
    anchor_role = str((compose_anchor_row or {}).get("role") or intent.anchor_role or "")

    # 提取锚点上下文，供 ES 查询生成使用（虚拟锚点跳过）
    _anchor_title = str((compose_anchor_row or {}).get("title") or "").strip()
    _anchor_cat2 = str((compose_anchor_row or {}).get("category_l2") or "").strip()
    if not _anchor_cat2 and intent.category:
        _anchor_cat2 = str(intent.category[0]).strip()
    if not _anchor_title and _anchor_cat2:
        _anchor_title = _anchor_cat2
    _is_virtual = (compose_anchor_row or {}).get("_is_virtual_image_anchor", False)
    anchor_context: dict[str, str] | None = None
    if (_anchor_title or _anchor_cat2) and not _is_virtual:
        anchor_context = {"title": _anchor_title, "category_l2": _anchor_cat2}
    enable_cat2 = bool(rec.get("enable_category_l2_pairing", True))
    enable_cs = bool(rec.get("enable_color_series_pairing", True))
    allowed_cat2 = resolve_pairing_allowed_companions(
        compose_anchor_row,
        intent_categories=list(intent.category or []),
        intent_override_categories=list(intent.category or []),
    ) if enable_cat2 else None
    # 色系过滤：intent 色系（无方向，对称规则）或锚点色系（按 anchor→companion 方向
    # 取 fila_sku 方向化 YAML：上装→下装 与 下装→上装 不对称）。direction 与 target
    # role 相关，故在 _process_one_role 内按 role 计算。
    user_cs2 = list(intent.color_series or []) if enable_cs else []
    anchor_cs2 = resolve_anchor_color_series(compose_anchor_row) if enable_cs else None

    raw_by_role: dict[str, list[dict[str, Any]]] = {}
    role_meta: dict[str, Any] = {}

    def _process_one_role(role: str) -> tuple[str, list[dict[str, Any]], dict[str, Any]] | None:
        """Parallel worker: resolve ES query + search + fetch SKU rows for one role."""
        if compose_anchor_id and anchor_role and role == anchor_role \
                and not (compose_anchor_row or {}).get("_is_virtual_image_anchor"):
            return None
        if not enable_cs:
            allowed_cs_role: list[str] | None = None
        elif user_cs2:
            allowed_cs_role = get_companion_color_series(user_cs2[0]) or user_cs2
        else:
            allowed_cs_role = (
                get_companion_color_series(
                    anchor_cs2,
                    anchor_role=anchor_role,
                    companion_role=role,
                ) if anchor_cs2 else None
            )
        relax_enabled, relax_priority, relax_min_hits = get_relax_config()
        # search_fn 闭包：按 dropped 集 build query → 搜索 → 物化 + 冲突过滤 + 去重。
        # last 记录最后一次实际使用的 query/meta（可观测性，避免再调一次 resolve）。
        last: dict[str, Any] = {"es_query": None, "meta": None}

        def _pick_from_hits(
            hits: list[tuple[str, float]],
            seen_in_role: set[str],
        ) -> list[dict[str, Any]]:
            """把 ES hits 物化成 SKU 行 + 冲突过滤 + 去重 + _es_score。"""
            picked_local: list[dict[str, Any]] = []
            for sid, score in hits:
                if sid in seen_in_role:
                    continue
                row = sku_r.get_sku(sid)
                if not row:
                    continue
                if compose_anchor_row and check_companion_conflict(
                    compose_anchor_row, row,
                    bypass_all=role_has_explicit_positive(intent, role),
                ):
                    logger.info(
                        "[query2es·冲突过滤] anchor=%s 与 sku=%s "
                        "category_l2=%s title=%s 属性冲突，跳过",
                        compose_anchor_id, sid, row.get("category_l2"), row.get("title"),
                    )
                    continue
                seen_in_role.add(sid)
                if compose_anchor_id and sid == compose_anchor_id:
                    continue
                c = dict(row)
                c["_es_score"] = float(score)
                picked_local.append(c)
            return picked_local

        def _search_fn(dropped: set[str]) -> list[dict[str, Any]]:
            es_q, meta_inner = resolve_es_query_for_role(
                intent,
                role,
                index_name=index_name,
                llm_enabled=llm_on,
                image_base64=image_base64,
                model_override=model_override,
                anchor_context=anchor_context,
                anchor_row=compose_anchor_row,
                **_es_relax_kwargs(
                    dropped,
                    allowed_cat2=allowed_cat2,
                    allowed_cs_role=allowed_cs_role,
                    enable_cs=enable_cs,
                ),
            )
            last["es_query"] = es_q
            last["meta"] = meta_inner
            hits = sku_r._es.search_skus_with_query(es_q, per_role)  # noqa: SLF001
            return _pick_from_hits(hits, seen_in_role)

        seen_in_role: set[str] = set()
        if relax_enabled:
            picked, dropped_list = run_with_progressive_relax(
                _search_fn, relax_priority, relax_min_hits,
            )
        else:
            # master switch off：单次查询，无放宽（回退旧行为）
            picked = _search_fn(set())
            dropped_list = []

        meta = last["meta"] or {}
        meta["es_query"] = last["es_query"]
        meta["hits"] = len(picked)
        if dropped_list:
            meta["fallback_dropped"] = dropped_list
        log_text_search_recall_io(
            trace_id=trace_id,
            entity="sku",
            channel="query2es",
            query=str(meta.get("fallback_q") or intent.text or "")[:500],
            limit=per_role,
            output_ids=[r.get("sku_id") for r in picked],
            extra={
                "index": index_name,
                "target_role": role,
                "es_query_source": meta.get("source"),
                "fallback_dropped": dropped_list,
            },
        )
        return role, picked, meta

    # Parallelize across roles (each role: LLM query gen + ES search + SKU fetch)
    role_workers = min(len(roles), 3)
    if role_workers > 1:
        with ThreadPoolExecutor(max_workers=role_workers) as pool:
            futures = {pool.submit(_process_one_role, r): r for r in roles}
            for fut in as_completed(futures):
                result = fut.result()
                if result is None:
                    continue
                role, picked, meta = result
                role_meta[role] = meta
                if picked:
                    raw_by_role[role] = picked
    else:
        for role in roles:
            result = _process_one_role(role)
            if result is None:
                continue
            role, picked, meta = result
            role_meta[role] = meta
            if picked:
                raw_by_role[role] = picked

    raw_sku_n = sum(len(v) for v in raw_by_role.values())
    by_role_rows = _dedupe_role_recall_skus(
        raw_by_role,
        compose_anchor_id,
        score_field="_es_score",
    )
    deduped_sku_n = sum(len(v) for v in by_role_rows.values())

    if _debug_recall_io_enabled():
        log_flow(
            "query2es_sku_dedupe",
            {
                "trace_id": trace_id,
                "compose_anchor_sku_id": compose_anchor_id,
                "sku_before_dedupe": raw_sku_n,
                "sku_after_dedupe": deduped_sku_n,
                "roles": {
                    r: [x.get("sku_id") for x in rows]
                    for r, rows in by_role_rows.items()
                },
                "role_meta": role_meta,
            },
        )
    return by_role_rows, role_meta


def recall_query2es_composed_outfits(
    sku_r: SkuRetriever,
    intent: UserIntent,
    compose_anchor_row: dict[str, Any] | None,
    *,
    image_base64: str | None = None,
    trace_id: str | None = None,
    model_override: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """通路3·per_channel 模式：Query2ES 召回 SKU（去重）→ 直接拼套。

    Returns:
        (composed_outfits, es_debug) — es_debug 包含各 role 的 ES query 与 meta。

    global 模式下不调用本函数，改由 multi_path_recall 聚合进全局池统一拼套。
    """
    cfg = load_config()
    rec = cfg.get("recommend") or {}
    per_role = es_top_n_per_role()
    max_outfits = recall_outfit_limit(cfg)

    by_role_rows, role_meta = recall_query2es_skus(
        sku_r, intent, compose_anchor_row,
        image_base64=image_base64, trace_id=trace_id, model_override=model_override,
    )

    anchor_for_compose: dict[str, Any] | None = None
    if compose_anchor_row:
        anchor_for_compose = dict(compose_anchor_row)
        anchor_for_compose["_is_image_input_anchor"] = True

    composed = compose_outfits_from_role_recall(
        anchor_for_compose,
        by_role_rows,
        max_outfits=max_outfits,
        picks_per_role=per_role,
        source="query2es_compose",
    )
    for o in composed:
        o["source"] = "query2es_compose"
        o["_recall_path"] = RecallPathway.OUTFIT_QUERY2ES_COMPOSE.value
    return composed, role_meta


def _outfit_text_vector_score(outfit: dict[str, Any]) -> float:
    sims: list[float] = []
    for it in outfit.get("items") or []:
        v = it.get("_text_vector_sim")
        if v is not None:
            sims.append(float(v))
    if sims:
        return sum(sims) / len(sims)
    return float(outfit.get("_text_vector_score") or 0.35)


def _outfit_es_compose_score(outfit: dict[str, Any]) -> float:
    scores: list[float] = []
    for it in outfit.get("items") or []:
        v = it.get("_es_score")
        if v is not None:
            scores.append(float(v))
    if scores:
        return sum(scores) / len(scores)
    v2 = outfit.get("_es_score")
    if v2 is not None:
        return float(v2)
    return 0.4


def _outfit_pool_score(outfit: dict[str, Any]) -> float:
    """全局池合成搭配的 RRF 融合分（item _pool_score 均值，回退 outfit 级）。"""
    scores: list[float] = []
    for it in outfit.get("items") or []:
        v = it.get("_pool_score")
        if v is not None:
            scores.append(float(v))
    if scores:
        return sum(scores) / len(scores)
    v = outfit.get("_pool_score")
    return float(v) if v is not None else 0.0


def _outfit_compose_score(outfit: dict[str, Any]) -> float:
    """合成搭配的统一排分：global 模式用 pool 分，per_channel 用文本向量分。

    global 模式下 compose_outfit_from_items 会给 outfit 写入 float 型 ``_pool_score``；
    per_channel 模式该字段为 None，回退到 ``_outfit_text_vector_score``。
    """
    if outfit.get("_pool_score") is not None:
        return _outfit_pool_score(outfit)
    return _outfit_text_vector_score(outfit)


def _composed_stream_pathway(
    composed_outfits: list[dict[str, Any]],
) -> RecallPathway:
    """合成流的整体通路标签：global 模式为 GLOBAL_COMPOSE，否则 TEXT_VECTOR_COMPOSE。"""
    for o in composed_outfits:
        rp = str(o.get("_recall_path") or "")
        if rp == RecallPathway.OUTFIT_GLOBAL_COMPOSE.value:
            return RecallPathway.OUTFIT_GLOBAL_COMPOSE
        if rp:
            return RecallPathway(rp)
    return RecallPathway.OUTFIT_TEXT_VECTOR_COMPOSE


def multi_path_recall(
    store: LocalDataStore | DataFacade,
    sku_r: SkuRetriever,
    intent: UserIntent,
    anchor: str | None,
    compose_anchor_row: dict[str, Any] | None,
    *,
    image_base64: str | None = None,
    trace_id: str | None = None,
    model_override: str | None = None,
    candidate_skus: list[str] | None = None,
    milvus: Any | None = None,
) -> dict[str, Any]:
    """多路召回并行执行，返回各路结果与耗时。

    candidate_skus: 全部 Milvus 召回 SKU（含 anchor），用于 image_vector
    通路批量查固定搭配库，一次 ES terms 查询拿到所有搭配。

    compose_mode（config recommend.compose_mode）:
    - ``per_channel``（默认）: text_vector / query2es / complementary_model 各自
      召回 SKU 后**在通道内**拼套，下游 RRF 融合各路搭配。
    - ``global``: 三路只召回 per-role SKU → 聚合进全局候选池（RRF 融合多路分数）
      → 统一拼套一次。某路缺某 role 时其他路的同 role 单品可补，避免好单品被埋。
      anchor_graph 路（固定搭配库）不受影响，仍在 outfit 级与全局合成搭配去重合并。
    """
    from time import perf_counter

    cfg = load_config()
    rec = cfg.get("recommend") or {}
    compose_mode = str(rec.get("compose_mode") or "per_channel")
    global_mode = compose_mode == "global"

    path_sw = get_outfit_recall_path_switches()
    results: dict[str, Any] = {
        "graph_outfits": [],
        "src_ids": set(),
        "composed_outfits": [],
        "query2es_outfits": [],
        "complementary_outfits": [],
        "es_debug": {},
        "pool_debug": {},
        "timings": {},
    }

    compose_anchor_id = str((compose_anchor_row or {}).get("sku_id") or anchor or "")

    def _run_image_vector() -> tuple[list[dict[str, Any]], set[str]]:
        if path_sw.get("image_vector", True) and anchor:
            return recall_anchor_graph_outfits(
                store, anchor,
                candidate_skus=candidate_skus,
                intent_gender=intent.gender,
                intent_season=list(intent.season or []),
                intent_target_roles=list(intent.target_roles or []),
                anchor_attrs=compose_anchor_row,
                intent=intent,
            )
        return [], set()

    if not global_mode:
        # --- per_channel: 各路召回+拼套 ---
        def _run_text_vector() -> list[dict[str, Any]]:
            if path_sw.get("text_vector", True):
                return recall_text_vector_composed_outfits(
                    sku_r, intent, compose_anchor_row, trace_id=trace_id,
                )
            return []

        def _run_query2es() -> tuple[list[dict[str, Any]], dict[str, Any]]:
            if path_sw.get("query2es", True):
                return recall_query2es_composed_outfits(
                    sku_r, intent, compose_anchor_row,
                    image_base64=image_base64, trace_id=trace_id,
                    model_override=model_override,
                )
            return [], {}

        def _run_complementary_model() -> list[dict[str, Any]]:
            if path_sw.get("complementary_model", False) and compose_anchor_row and milvus:
                return recall_complementary_composed_outfits(
                    sku_r, milvus, intent, compose_anchor_row,
                    trace_id=trace_id,
                )
            return []
    else:
        # --- global: 各路只召回 SKU，统一进全局池拼套 ---
        def _run_text_vector() -> dict[str, list[dict[str, Any]]]:
            if path_sw.get("text_vector", True):
                return recall_text_vector_skus(
                    sku_r, intent, compose_anchor_row, trace_id=trace_id,
                )
            return {}

        def _run_query2es() -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
            if path_sw.get("query2es", True):
                return recall_query2es_skus(
                    sku_r, intent, compose_anchor_row,
                    image_base64=image_base64, trace_id=trace_id,
                    model_override=model_override,
                )
            return {}, {}

        def _run_complementary_model() -> dict[str, list[dict[str, Any]]]:
            if path_sw.get("complementary_model", False) and compose_anchor_row and milvus:
                return recall_complementary_skus(
                    sku_r, milvus, intent, compose_anchor_row,
                    trace_id=trace_id,
                )
            return {}

    with ThreadPoolExecutor(max_workers=4) as pool:
        t0 = perf_counter()
        futures = {
            pool.submit(_run_image_vector): "image_vector",
            pool.submit(_run_text_vector): "text_vector",
            pool.submit(_run_query2es): "query2es",
            pool.submit(_run_complementary_model): "complementary_model",
        }
        tv_res: Any = None
        q2es_res: Any = None
        comp_res: Any = None
        for fut in as_completed(futures):
            path_name = futures[fut]
            elapsed = int((perf_counter() - t0) * 1000)
            try:
                res = fut.result()
            except Exception:
                logger.exception("recall path %s failed", path_name)
                if path_name == "image_vector":
                    res = ([], set())
                elif path_name == "query2es":
                    res = ([], {}) if not global_mode else ({}, {})
                else:
                    res = [] if not global_mode else {}

            if path_name == "image_vector":
                results["graph_outfits"], results["src_ids"] = res
                results["timings"]["image_vector"] = {
                    "elapsed_ms": elapsed,
                    "count": len(results["graph_outfits"]),
                    "unit": "outfits",
                }
            elif path_name == "text_vector":
                tv_res = res
                results["timings"]["text_vector"] = {
                    "elapsed_ms": elapsed,
                    "count": _res_count(res, global_mode),
                    "unit": "skus" if global_mode else "outfits",
                }
            elif path_name == "query2es":
                q2es_res = res
                if not global_mode:
                    results["es_debug"] = res[1] if isinstance(res, tuple) else {}
                else:
                    results["es_debug"] = res[1] if isinstance(res, tuple) else {}
                results["timings"]["query2es"] = {
                    "elapsed_ms": elapsed,
                    "count": _res_count(res, global_mode),
                    "unit": "skus" if global_mode else "outfits",
                }
            elif path_name == "complementary_model":
                comp_res = res
                results["timings"]["complementary_model"] = {
                    "elapsed_ms": elapsed,
                    "count": _res_count(res, global_mode),
                    "unit": "skus" if global_mode else "outfits",
                }

    if not global_mode:
        results["composed_outfits"] = tv_res or []
        if isinstance(q2es_res, tuple):
            results["query2es_outfits"] = q2es_res[0]
        results["complementary_outfits"] = comp_res or []
        results["compose_mode"] = "per_channel"
        results["composed_outfit_count"] = (
            len(results["composed_outfits"])
            + len(results["query2es_outfits"])
            + len(results["complementary_outfits"])
        )
        return results

    # --- global: 聚合三路 SKU 进全局池 → 统一拼套 ---
    per_channel_by_role: dict[str, dict[str, list[dict[str, Any]]]] = {}
    if isinstance(tv_res, dict) and tv_res:
        per_channel_by_role["text_vector"] = tv_res
    if isinstance(q2es_res, tuple) and q2es_res and q2es_res[0]:
        per_channel_by_role["query2es"] = q2es_res[0]
    if isinstance(comp_res, dict) and comp_res:
        per_channel_by_role["complementary_model"] = comp_res

    pool_by_role, pool_debug = build_candidate_pool(
        per_channel_by_role, compose_anchor_id,
    )
    # 全局去重 + per-(category_l2,color_series) / per-role 上限，按融合分截断
    pool_by_role = _dedupe_role_recall_skus(
        pool_by_role, compose_anchor_id, score_field="_pool_score",
    )
    results["pool_debug"] = pool_debug
    # 全局池召回商品数（去重+cap 后的唯一 SKU 数）与组合搭配数，供前端展示
    results["recalled_sku_count"] = sum(len(v) for v in pool_by_role.values())
    results["compose_mode"] = "global"
    # per-role 召回/去重计数（before=池子聚合后、cap 前；after=cap 后），供前端分 role 展示
    role_debug = pool_debug.get("roles") or {}
    pool_role_counts: dict[str, dict[str, Any]] = {}
    for role, rows in pool_by_role.items():
        rd = role_debug.get(role) or {}
        pool_role_counts[role] = {
            "before": int(rd.get("total") or 0),
            "after": len(rows),
            "channels": rd.get("by_channel") or {},
        }
    results["pool_role_counts"] = pool_role_counts

    max_outfits = recall_outfit_limit(cfg)
    per_role = int(rec.get("default_sku_per_role") or 3)
    anchor_for_compose: dict[str, Any] | None = None
    if compose_anchor_row:
        anchor_for_compose = dict(compose_anchor_row)
        anchor_for_compose["_is_image_input_anchor"] = True

    composed = compose_outfits_from_role_recall(
        anchor_for_compose,
        pool_by_role,
        max_outfits=max_outfits,
        picks_per_role=per_role,
        source="global_compose",
    )
    for o in composed:
        o["source"] = "global_compose"
        o["_recall_path"] = RecallPathway.OUTFIT_GLOBAL_COMPOSE.value
    results["composed_outfits"] = composed
    results["composed_outfit_count"] = len(composed)
    # global 模式下 query2es/complementary 已并入全局池，无独立搭配流；
    # query2es 的 role_meta 仍保留在 es_debug 供 trace。
    results["query2es_outfits"] = []
    results["complementary_outfits"] = []
    logger.info(
        "[multi_path_recall·global] anchor=%s channels=%s pool_roles=%s "
        "pool_skus=%d composed=%d",
        compose_anchor_id, list(per_channel_by_role.keys()),
        {r: len(v) for r, v in pool_by_role.items()},
        sum(len(v) for v in pool_by_role.values()), len(composed),
    )
    return results


def _res_count(res: Any, global_mode: bool) -> int:
    """统一计数 per_channel（outfit 数）与 global（SKU 数）的路结果。"""
    if res is None:
        return 0
    if isinstance(res, tuple):
        r0 = res[0] if res else None
    else:
        r0 = res
    if not r0:
        return 0
    if isinstance(r0, list):
        return len(r0)
    if isinstance(r0, dict):
        return sum(len(v) for v in r0.values() if isinstance(v, list))
    return 0



def _outfit_complementary_score(outfit: dict[str, Any]) -> float:
    sims: list[float] = []
    for it in outfit.get("items") or []:
        v = it.get("_complementary_sim")
        if v is not None:
            sims.append(float(v))
    if sims:
        return sum(sims) / len(sims)
    return 0.4


def merge_and_dedupe_outfits(
    graph_outfits: list[dict[str, Any]],
    composed_outfits: list[dict[str, Any]],
    *,
    query2es_outfits: list[dict[str, Any]] | None = None,
    complementary_outfits: list[dict[str, Any]] | None = None,
    anchor_sku_id: str,
    source_match_ids: set[str],
    anchor_vector_sim: float = 0.0,
) -> tuple[list[dict[str, Any]], dict[str, float], RecallPathway]:
    """合并多路召回、过滤、去重，返回 (deduped, vec_map, pathway)。

    使用 Reciprocal Rank Fusion (RRF) 替代硬编码分数，统一不同通路的排名。
    顺序：RRF 打分 → 合并多路 → 去重（用 RRF 分数择优）。
    """
    es_list = list(query2es_outfits or [])
    comp_list = list(complementary_outfits or [])

    # --- RRF: 先按各通路内排名计算融合分数 ---
    RRF_K = 60
    path_rankings: list[list[str]] = []

    def _ranked_oids(outfits: list[dict[str, Any]], score_fn) -> list[str]:
        scored = [(score_fn(o), str(o.get("outfit_id") or "")) for o in outfits]
        scored.sort(key=lambda x: -x[0])
        return [oid for _, oid in scored if oid]

    if graph_outfits:
        path_rankings.append(_ranked_oids(
            [o for o in graph_outfits if len(o.get("items") or []) > 1],
            lambda o: max(anchor_vector_sim, 0.72)
            if str(o.get("outfit_id") or "") in source_match_ids
            else 0.45,
        ))
    if composed_outfits:
        path_rankings.append(_ranked_oids(
            [o for o in composed_outfits if len(o.get("items") or []) > 1],
            lambda o: _outfit_compose_score(o),
        ))
    if es_list:
        path_rankings.append(_ranked_oids(
            [o for o in es_list if len(o.get("items") or []) > 1],
            lambda o: _outfit_es_compose_score(o),
        ))
    if comp_list:
        path_rankings.append(_ranked_oids(
            [o for o in comp_list if len(o.get("items") or []) > 1],
            lambda o: _outfit_complementary_score(o),
        ))

    vec_map: dict[str, float] = {}
    for ranking in path_rankings:
        for rank, oid in enumerate(ranking, start=1):
            vec_map[oid] = vec_map.get(oid, 0.0) + 1.0 / (RRF_K + rank)

    # --- 合并多路 ---
    combined = list(graph_outfits) + list(composed_outfits) + es_list + comp_list
    combined = [o for o in combined if len(o.get("items") or []) > 1]
    if not combined:
        pw = RecallPathway.OUTFIT_TEXT_ES
        if graph_outfits:
            pw = RecallPathway.OUTFIT_ANCHOR_GRAPH
        elif es_list:
            pw = RecallPathway.OUTFIT_QUERY2ES_COMPOSE
        elif composed_outfits:
            pw = _composed_stream_pathway(composed_outfits)
        elif comp_list:
            pw = RecallPathway.OUTFIT_COMPLEMENTARY_MODEL
        return [], {}, pw

    # --- 去重：用 RRF 分数择优 ---
    deduped = dedupe_outfits_same_skus_prefer_anchor_master(
        combined,
        anchor_sku_id or "",
        source_match_ids=source_match_ids,
        vec_map=vec_map,
    )

    # 未在任何排名中出现的 deduped outfit 给最低分
    for o in deduped:
        oid = str(o.get("outfit_id") or "")
        if oid and oid not in vec_map:
            vec_map[oid] = 1.0 / (RRF_K + len(deduped))

    active = sum(
        1
        for part in (graph_outfits, composed_outfits, es_list, comp_list)
        if part
    )
    if active >= 2:
        pathway = RecallPathway.OUTFIT_DUAL_MERGED
    elif graph_outfits:
        pathway = RecallPathway.OUTFIT_ANCHOR_GRAPH
    elif es_list:
        pathway = RecallPathway.OUTFIT_QUERY2ES_COMPOSE
    elif composed_outfits:
        pathway = _composed_stream_pathway(composed_outfits)
    elif comp_list:
        pathway = RecallPathway.OUTFIT_COMPLEMENTARY_MODEL
    else:
        pathway = RecallPathway.OUTFIT_TEXT_VECTOR_COMPOSE
    return deduped, vec_map, pathway


def coarse_rank_outfits(
    deduped: list[dict[str, Any]],
    *,
    intent: UserIntent,
    source_match_ids: set[str],
    source_match_scores: dict[str, float] | None = None,
    coarse_limit: int | None = None,
    trace_id: str | None = None,
) -> list[dict[str, Any]]:
    """粗排：用规则打分截断候选集，减少送入 LLM 精排的数量。"""
    cfg = load_config()
    rec_cfg = cfg.get("recommend") or {}
    limit = coarse_limit or int(rec_cfg.get("coarse_rank_limit") or 20)

    ranked = rank_outfits(
        deduped,
        intent_gender=intent.gender,
        intent_season=list(intent.season or []),
        intent_tags=list(intent.occasion_tags or [])
        + list(intent.style_tags or []),
        source_match_ids=source_match_ids,
        source_match_scores=source_match_scores,
        budget_max=intent.budget_max,
    )
    coarse_top = [o for _, o in ranked[:limit]]
    if _debug_recall_io_enabled():
        log_flow(
            "coarse_rank",
            {
                "trace_id": trace_id,
                "input_count": len(deduped),
                "output_count": len(coarse_top),
                "limit": limit,
            },
        )
    return coarse_top


def rank_deduped_outfits(
    deduped: list[dict[str, Any]],
    *,
    intent: UserIntent,
    source_match_ids: set[str],
    source_match_scores: dict[str, float] | None = None,
    limit: int | None = None,
    trace_id: str | None = None,
    scoring_method_override: str | None = None,
    model_override: str | None = None,
) -> list[dict[str, Any]]:
    """对去重后的搭配进行排序打分，返回 top raw outfit 行（未转 card）。"""
    from time import perf_counter as _pc  # noqa: PLC0415

    cfg = load_config()
    rec_cfg = cfg.get("recommend") or {}
    lim = limit or rank_outfit_limit(cfg)
    scoring_method = scoring_method_override or str(rec_cfg.get("ranking_scoring_method") or "rule")

    _t_rank_start = _pc()
    if scoring_method == "llm":
        # enable_llm_rank_reason 时的后端选择：
        #   ranking_llm    = 现有文本模型 batch/parallel 打分+理由（合并）
        #   partner        = 私有部署裸 vLLM 多模态模型逐套并行打分+理由（合并）
        #   partner_qwen  = 排序走 partner vLLM、推荐理由走 qwen3.5-flash，两步并行
        #   partner_vision = 排序走 partner vLLM、推荐理由走 qwen3.6-27b(vision_llm)
        #                    看单品 tryon_image 生成理由，两步并行
        # partner / partner_qwen / partner_vision 的排序都走 partner_rank_outfits；
        # partner_qwen / partner_vision 的理由由 chat_stream 另起 LLM 并行生成，
        # 不取 partner 的 _llm_reason。
        rank_method = str(rec_cfg.get("llm_rank_reason_method") or "ranking_llm")
        if rank_method in ("partner", "partner_qwen", "partner_vision"):
            # partner 模型与文本 ranking_llm 是不同部署，不可被 req.llm_model
            # （文本模型名，如 qwen3.5-flash）覆盖，否则 vLLM 404。始终用 config
            # partner_rank_reason.model（如 fila-outfit-v6_1）。
            ranked = partner_rank_outfits(
                deduped,
                cfg=cfg,
                model_override=None,
            )
        else:
            llm_mode = str(rec_cfg.get("ranking_llm_mode") or "batch")
            llm_workers = int(rec_cfg.get("ranking_llm_max_workers") or 5)
            ranked = llm_rank_outfits(
                deduped,
                mode=llm_mode,
                max_workers=llm_workers,
                model_override=model_override,
            )
    else:
        ranked = rank_outfits(
            deduped,
            intent_gender=intent.gender,
            intent_season=list(intent.season or []),
            intent_tags=list(intent.occasion_tags or [])
            + list(intent.style_tags or []),
            source_match_ids=source_match_ids,
            source_match_scores=source_match_scores,
            budget_max=intent.budget_max,
        )
    _ranking_elapsed_ms = int((_pc() - _t_rank_start) * 1000)
    if _debug_recall_io_enabled():
        log_flow(
            "outfit_ranking",
            {
                "trace_id": trace_id,
                "scoring_method": scoring_method,
                "llm_mode": rec_cfg.get("ranking_llm_mode") if scoring_method == "llm" else None,
                "llm_rank_reason_method": rec_cfg.get("llm_rank_reason_method") if scoring_method == "llm" else None,
                "input_count": len(deduped),
                "elapsed_ms": _ranking_elapsed_ms,
            },
        )
    log_outfit_rank_scores(
        ranked,
        trace_id=trace_id,
        top_k=lim,
    )
    top = []
    for idx, (score, o) in enumerate(ranked[:lim], start=1):
        row = dict(o)
        oid = str(o.get("outfit_id") or "")
        if scoring_method == "llm":
            # LLM 打分时，breakdown 展示 LLM 的结果
            breakdown = {
                "total": round(float(score), 4),
                "scoring_method": "llm",
                "items": [
                    {
                        "key": "llm_aesthetic",
                        "label": "LLM美学打分",
                        "weight": 1.0,
                        "raw": round(float(score), 4),
                        "weighted": round(float(score), 4),
                        "brief": str(o.get("_llm_brief") or ""),
                    },
                ],
            }
        else:
            in_src = oid in source_match_ids
            src_raw = (source_match_scores or {}).get(oid, 1.0) if in_src else 0.3
            breakdown = compute_outfit_rank_breakdown(
                o,
                intent_gender=intent.gender,
                intent_season=list(intent.season or []),
                intent_tags=list(intent.occasion_tags or [])
                + list(intent.style_tags or []),
                in_source_match=in_src,
                source_match_raw=src_raw,
                budget_max=intent.budget_max,
            )
        row["_rank_score"] = round(float(score), 4)
        row["_rank_breakdown"] = breakdown
        row["_rank_order"] = idx
        row["_ranking_scoring_method"] = scoring_method
        row["_ranking_elapsed_ms"] = _ranking_elapsed_ms
        top.append(row)

    return top


def merge_and_rank_outfits(
    graph_outfits: list[dict[str, Any]],
    composed_outfits: list[dict[str, Any]],
    *,
    query2es_outfits: list[dict[str, Any]] | None = None,
    complementary_outfits: list[dict[str, Any]] | None = None,
    intent: UserIntent,
    anchor_sku_id: str,
    source_match_ids: set[str],
    anchor_vector_sim: float = 0.0,
    limit: int | None = None,
    trace_id: str | None = None,
) -> tuple[list[dict[str, Any]], RecallPathway]:
    """合并多路、去重、排序，返回 raw outfit 行（未转 card）。

    兼容旧调用方式，内部委托 merge_and_dedupe_outfits + rank_deduped_outfits。
    """
    deduped, _vec_map, pathway = merge_and_dedupe_outfits(
        graph_outfits,
        composed_outfits,
        query2es_outfits=query2es_outfits,
        complementary_outfits=complementary_outfits,
        anchor_sku_id=anchor_sku_id,
        source_match_ids=source_match_ids,
        anchor_vector_sim=anchor_vector_sim,
    )
    if not deduped:
        return [], pathway
    top = rank_deduped_outfits(
        deduped,
        intent=intent,
        source_match_ids=source_match_ids,
        limit=limit,
        trace_id=trace_id,
    )
    return top, pathway
