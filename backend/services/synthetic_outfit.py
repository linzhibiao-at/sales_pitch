"""将锚点 SKU 与关系召回的互补 SKU 合成最小套装结构（供排序与 outfit_card）。"""

from __future__ import annotations

import hashlib
import itertools
import logging
from typing import Any, Dict, List, Set

from backend.config import load_config
from backend.intent.slot_defs import role_display_priority
from backend.models import normalize_gender_first, normalize_season

logger = logging.getLogger(__name__)

from backend.ranking.scoring import (
    outfit_completeness_score,
    tryon_coverage_from_items,
)


def _item_from_sku_row(
    row: Dict[str, Any],
    *,
    is_master: bool,
    is_anchor: bool,
) -> Dict[str, Any]:
    return {
        "sku_id": row.get("sku_id"),
        "spu_id": row.get("spu_id"),
        "role": row.get("role"),
        "is_master": is_master,
        "is_anchor": is_anchor,
        "title": row.get("title"),
        "price": row.get("price"),
        "display_image": row.get("display_image"),
        "tryon_image": row.get("tryon_image"),
        "image_quality": row.get("image_quality") or {},
    }


def _item_sku_id(item: Dict[str, Any]) -> str:
    """从搭配 item 提取 SKU ID（兼容 ES 原始字段 attrAlias/idAlias）。"""
    raw = (
        item.get("sku_id")
        or item.get("skuId")
        or item.get("attrAlias")
        or item.get("idAlias")
    )
    return str(raw).strip() if raw is not None else ""


def order_outfit_items_by_role(
    items: List[Dict[str, Any]],
    anchor_sku_id: str = "",
) -> List[Dict[str, Any]]:
    """搭配内单品按 role 展示优先级排序：上装 > 下装/连衣裙 > 鞋子 > 配件，
    未知角色垫底。同优先级内保持原相对顺序（Python 稳定排序）。

    不再把输入商品(anchor)强制放首位——顺序完全由 role 决定。
    anchor_sku_id 参数保留以兼容调用方；anchor 的 is_master/is_anchor 标记
    仍由调用方设置，供前端高亮与去重/排序使用，与展示位置无关。
    """
    clean = [it for it in items if isinstance(it, dict)]
    clean.sort(
        key=lambda it: role_display_priority(it.get("role")),
    )
    return clean


def pair_outfit_from_anchor_and_target(
    anchor_row: Dict[str, Any],
    target_row: Dict[str, Any],
    *,
    relation_ids: List[str],
) -> Dict[str, Any]:
    """两件式套装：锚点为主商品，互补 SKU 为第二件。"""
    aid = str(anchor_row.get("sku_id") or "")
    tid = str(target_row.get("sku_id") or "")
    raw_id = f"synth_rel_{aid}_{tid}"
    oid = f"synth_rel_{hashlib.md5(raw_id.encode()).hexdigest()[:8]}"
    items = [
        _item_from_sku_row(anchor_row, is_master=True, is_anchor=True),
        _item_from_sku_row(target_row, is_master=False, is_anchor=False),
    ]
    roles_set: Set[str] = set()
    for it in items:
        r = str(it.get("role") or "")
        if r:
            roles_set.add(r)
    roles = sorted(roles_set)
    price_total = float(anchor_row.get("price") or 0.0) + float(
        target_row.get("price") or 0.0,
    )
    comp = outfit_completeness_score(roles_set)
    tryon = tryon_coverage_from_items(items)
    title_a = str(anchor_row.get("title") or aid)
    title_t = str(target_row.get("title") or tid)
    gender = normalize_gender_first(anchor_row.get("gender") or target_row.get("gender"))
    season = normalize_season(anchor_row.get("season") or target_row.get("season"))
    return {
        "outfit_id": oid,
        "name": f"锚点组合 · {title_a} + {title_t}",
        "master_sku_id": aid,
        "master_spu_id": anchor_row.get("spu_id"),
        "items": items,
        "roles": roles,
        "gender": gender,
        "season": season,
        "price_total": price_total,
        "display_image": str(anchor_row.get("display_image") or "").strip()
        or str(target_row.get("display_image") or "").strip(),
        "index_images": anchor_row.get("index_images") or [],
        "background_img": "",
        "outfit_completeness_score": comp,
        "tryon_coverage": tryon,
        "source": "relation_pair",
        "source_relation_ids": list(dict.fromkeys(relation_ids)),
    }


def compose_outfit_from_items(
    item_rows: List[Dict[str, Any]],
    *,
    anchor_sku_id: str = "",
    source: str = "text_vector_compose",
) -> Dict[str, Any]:
    """将多件 SKU 行合成为一套搭配（锚点为主商品）。"""
    if not item_rows:
        return {}
    preferred_anchor: Dict[str, Any] | None = None
    for row in item_rows:
        if row.get("_is_image_input_anchor"):
            preferred_anchor = row
            break
    anchor_id = anchor_sku_id or str(
        (preferred_anchor or item_rows[0]).get("sku_id") or "",
    )
    items: List[Dict[str, Any]] = []
    roles_set: Set[str] = set()
    price_total = 0.0
    anchor_row: Dict[str, Any] | None = preferred_anchor
    for row in item_rows:
        sid = str(row.get("sku_id") or "")
        is_anchor = bool(anchor_id and sid == anchor_id)
        if is_anchor:
            anchor_row = row
        item = _item_from_sku_row(
            row,
            is_master=is_anchor,
            is_anchor=is_anchor,
        )
        if row.get("_text_vector_sim") is not None:
            item["_text_vector_sim"] = float(row["_text_vector_sim"])
        if row.get("_es_score") is not None:
            item["_es_score"] = float(row["_es_score"])
        if row.get("_pool_score") is not None:
            item["_pool_score"] = float(row["_pool_score"])
            item["_contributing_pathways"] = list(row.get("_contributing_pathways") or [])
        items.append(item)
        role = str(row.get("role") or "")
        if role:
            roles_set.add(role)
        price_total += float(row.get("price") or 0.0)
    if not anchor_row:
        anchor_row = item_rows[0]
        anchor_id = str(anchor_row.get("sku_id") or "")
        items[0] = _item_from_sku_row(
            anchor_row,
            is_master=True,
            is_anchor=True,
        )
    sku_part = "_".join(
        sorted(str(it.get("sku_id") or "") for it in items)[:6],
    )
    raw_id = f"synth_txt_{sku_part}"
    oid = f"synth_txt_{hashlib.md5(raw_id.encode()).hexdigest()[:8]}"
    title_a = str(anchor_row.get("title") or anchor_id)
    comp = outfit_completeness_score(roles_set)
    tryon = tryon_coverage_from_items(items)
    gender = normalize_gender_first(anchor_row.get("gender"))
    season = normalize_season(anchor_row.get("season"))
    sims = [
        float(it.get("_text_vector_sim"))
        for it in items
        if it.get("_text_vector_sim") is not None
    ]
    es_scores = [
        float(it.get("_es_score"))
        for it in items
        if it.get("_es_score") is not None
    ]
    text_score = sum(sims) / len(sims) if sims else 0.35
    es_score = sum(es_scores) / len(es_scores) if es_scores else 0.0
    pool_scores = [
        float(it.get("_pool_score"))
        for it in items
        if it.get("_pool_score") is not None
    ]
    pool_score = sum(pool_scores) / len(pool_scores) if pool_scores else None
    for it in items:
        row_sim = it.get("_text_vector_sim")
        if row_sim is not None:
            it["_text_vector_sim"] = float(row_sim)
    no_outfit_image = source in (
        "text_vector_compose",
        "query2es_compose",
        "complementary_model_compose",
        "global_compose",
    )
    name_prefix = "文本向量组合"
    if source == "query2es_compose":
        name_prefix = "Query2ES 组合"
    elif source == "complementary_model_compose":
        name_prefix = "互补模型组合"
    elif source == "global_compose":
        name_prefix = "全局候选池组合"
    # 搭配内单品排序：anchor 首位，其余按 role 优先级
    items = order_outfit_items_by_role(items, anchor_id)
    return {
        "outfit_id": oid,
        "name": f"{name_prefix} · {title_a} 等{len(items)}件",
        "master_sku_id": anchor_id,
        "master_spu_id": anchor_row.get("spu_id"),
        "items": items,
        "roles": sorted(roles_set),
        "gender": gender,
        "season": season,
        "price_total": price_total,
        "display_image": ""
        if no_outfit_image
        else str(anchor_row.get("display_image") or "").strip(),
        "index_images": [] if no_outfit_image else (
            anchor_row.get("index_images") or []
        ),
        "background_img": "",
        "_text_vector_score": text_score,
        "_es_score": es_score if es_scores else None,
        "_pool_score": pool_score,
        "outfit_completeness_score": comp,
        "tryon_coverage": tryon,
        "source": source,
        "source_relation_ids": [],
    }


def _sku_score(row: Dict[str, Any]) -> float:
    """取 SKU 行上的召回分数，用于桶内排序。

    优先级：全局池融合分 ``_pool_score`` > 文本向量分 > ES 分。
    全局池模式下所有行都带 ``_pool_score``（RRF 值，跨路可比）；
    per_channel 模式行不带 ``_pool_score``，回退到各路原始分。
    """
    for field in ("_pool_score", "_text_vector_sim", "_es_score"):
        v = row.get(field)
        if v is not None:
            return float(v)
    return 0.0


def _bucket_best_score(bucket: List[Dict[str, Any]]) -> float:
    """桶内最高分，用于桶间排序。"""
    return max((_sku_score(r) for r in bucket), default=0.0)


def _compose_diverse_greedy(
    anchor_row: Dict[str, Any] | None,
    role_candidates: dict[str, list[Dict[str, Any]]],
    roles: list[str],
    *,
    anchor_id: str,
    max_outfits: int,
    source: str,
) -> list[Dict[str, Any]]:
    """分桶轮选 + 贪心逐套构建：保证 target_role 商品中类/色系多样性。

    每个 role 的候选按 (category_l2, color_series) 分桶，桶按最高分降序排列。
    逐套构建时，各 role 通过 round-robin 索引依次轮选不同桶的代表 SKU。
    """
    # 1) 分桶：每个 role 按 (category_l2, color_series) 分组
    role_buckets: dict[str, list[list[Dict[str, Any]]]] = {}
    for role in roles:
        candidates = role_candidates.get(role, [])
        buckets: dict[tuple[str, str], list[Dict[str, Any]]] = {}
        for row in candidates:
            key = (
                str(row.get("category_l2") or "").strip(),
                str(row.get("color_series") or "").strip(),
            )
            buckets.setdefault(key, []).append(row)
        # 桶内按分数降序
        for rows in buckets.values():
            rows.sort(key=_sku_score, reverse=True)
        # 桶按最高分降序排列
        sorted_buckets = sorted(
            buckets.values(),
            key=_bucket_best_score,
            reverse=True,
        )
        role_buckets[role] = sorted_buckets
        logger.debug(
            "[diverse_greedy] role=%s, buckets=%d, candidates=%d",
            role, len(sorted_buckets), len(candidates),
        )

    # 2) 贪心逐套构建
    outfits: list[Dict[str, Any]] = []
    seen_sigs: set[str] = set()
    # 各 role 独立的桶轮选索引
    role_bucket_idx: dict[str, int] = {r: 0 for r in roles}
    # 各 role 桶内已消耗的 SKU 偏移（当桶内有多个 SKU 时，轮完一圈后用下一个）
    role_bucket_inner_idx: dict[str, dict[int, int]] = {r: {} for r in roles}
    # 最大尝试次数 = max_outfits 的 3 倍，防止死循环（seen_sigs 兜底去重）
    max_attempts = max_outfits * 3

    for attempt in range(max_attempts):
        if len(outfits) >= max_outfits:
            break
        combo_rows: list[Dict[str, Any]] = []
        if anchor_row:
            combo_rows.append(anchor_row)
        valid = True
        for role in roles:
            buckets = role_buckets.get(role, [])
            if not buckets:
                valid = False
                break
            bucket_idx = role_bucket_idx[role] % len(buckets)
            bucket = buckets[bucket_idx]
            inner_offsets = role_bucket_inner_idx[role]
            raw_idx = inner_offsets.get(bucket_idx, 0)
            # 稀缺 role 循环复用：桶用尽后回到首位（raw_idx % len(bucket)），
            # 让富余 role 继续轮选产出新搭配——同一件裤配不同鞋本就是不同 look。
            # 修复前这里 inner_idx>=len 即 valid=False 终止，导致 2 裤×51 鞋只产 2 套。
            inner_idx = raw_idx % len(bucket) if bucket else 0
            combo_rows.append(bucket[inner_idx])
            inner_offsets[bucket_idx] = raw_idx + 1
            role_bucket_idx[role] += 1
        if not valid:
            continue
        sig = "|".join(sorted(str(r.get("sku_id") or "") for r in combo_rows))
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)
        o = compose_outfit_from_items(
            combo_rows,
            anchor_sku_id=anchor_id,
            source=source,
        )
        if o:
            outfits.append(o)

    return outfits


def _compose_cartesian(
    anchor_row: Dict[str, Any] | None,
    role_candidates: dict[str, list[Dict[str, Any]]],
    roles: list[str],
    *,
    anchor_id: str,
    max_outfits: int,
    source: str,
) -> list[Dict[str, Any]]:
    """原始笛卡尔积组合（旧逻辑）。"""
    combos = itertools.product(
        *[role_candidates[r] for r in roles if r in role_candidates],
    )
    outfits: list[Dict[str, Any]] = []
    seen_sigs: set[str] = set()
    for combo in combos:
        rows: list[Dict[str, Any]] = []
        if anchor_row:
            rows.append(anchor_row)
        rows.extend(combo)
        sig = "|".join(sorted(str(r.get("sku_id") or "") for r in rows))
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)
        o = compose_outfit_from_items(
            rows,
            anchor_sku_id=anchor_id,
            source=source,
        )
        if o:
            outfits.append(o)
        if len(outfits) >= max_outfits:
            break
    return outfits


def compose_outfits_from_role_recall(
    anchor_row: Dict[str, Any] | None,
    by_role_rows: dict[str, list[Dict[str, Any]]],
    *,
    max_outfits: int = 6,
    picks_per_role: int = 2,
    source: str = "text_vector_compose",
) -> list[Dict[str, Any]]:
    """输入商品 + 各 target_role 召回 SKU，组合为成套搭配。

    compose_strategy 配置：
    - ``diverse_greedy``（默认）：分桶轮选，保证中类/色系多样性
    - ``cartesian``：笛卡尔积（旧逻辑）
    """
    roles = [r for r in by_role_rows if by_role_rows.get(r)]
    if not roles and not anchor_row:
        return []

    role_candidates: dict[str, list[Dict[str, Any]]] = {}
    anchor_id = str((anchor_row or {}).get("sku_id") or "")
    for role in roles:
        seen: set[str] = set()
        picked: list[Dict[str, Any]] = []
        for row in by_role_rows.get(role) or []:
            sid = str(row.get("sku_id") or "")
            if not sid or sid in seen:
                continue
            if anchor_id and sid == anchor_id:
                continue
            if str(row.get("role") or "") not in ("", role):
                continue
            seen.add(sid)
            picked.append(row)
            if len(picked) >= picks_per_role:
                break
        if picked:
            role_candidates[role] = picked

    if not role_candidates:
        if anchor_row:
            one = compose_outfit_from_items(
                [anchor_row],
                anchor_sku_id=anchor_id,
                source=source,
            )
            return [one] if one else []
        return []

    cfg = load_config().get("recommend") or {}
    strategy = str(cfg.get("compose_strategy") or "diverse_greedy")

    if strategy == "cartesian":
        return _compose_cartesian(
            anchor_row, role_candidates, roles,
            anchor_id=anchor_id, max_outfits=max_outfits, source=source,
        )
    return _compose_diverse_greedy(
        anchor_row, role_candidates, roles,
        anchor_id=anchor_id, max_outfits=max_outfits, source=source,
    )


def pair_outfits_from_relation_groups(
    anchor_row: Dict[str, Any],
    groups_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按角色分组内的顺序展平，按 target sku_id 去重。"""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for g in groups_rows:
        for row in g.get("rows") or []:
            if not isinstance(row, dict):
                continue
            tid = str(row.get("sku_id") or "")
            if not tid or tid in seen:
                continue
            seen.add(tid)
            rel = list(row.get("_source_relation_ids") or [])
            out.append(
                pair_outfit_from_anchor_and_target(
                    anchor_row,
                    row,
                    relation_ids=rel,
                ),
            )
    return out


def synth_card_to_doc(card: Dict[str, Any]) -> Dict[str, Any] | None:
    """把 outfit_card() 产出的合成搭配 card 投影为 outfits 索引 mapping 对齐的文档。

    仅当 card 的 outfit_id 以 ``synth_`` 开头（synth_txt_* / synth_rel_*）时投影，否则返回 None。
    严格对齐 scripts/build_fila_es_index.py:create_outfits_index 的 mapping，不引入新字段；
    不含 reason / _ 前缀分数 / outfit_completeness_score / tryon_coverage / gender / season /
    search_text（非 mapping 字段或 regenerate 不读）。source 取 card.recall_source
    （text_vector_compose 等，本就不在 OPERATIONAL_OUTFIT_SOURCES 内）。
    """
    oid = str(card.get("outfit_id") or "")
    if not oid.startswith("synth_"):
        return None
    items = card.get("items") or []
    roles = sorted(
        {str(it.get("role") or "") for it in items if str(it.get("role") or "").strip()}
    )
    return {
        "outfit_id": oid,
        "name": str(card.get("name") or ""),
        "source": str(card.get("recall_source") or ""),
        "roles": roles,
        "price_total": float(card.get("price_total") or 0.0),
        "display_image": str(card.get("display_image") or ""),
        "index_images": list(card.get("index_images") or []),
        "background_img": str(card.get("background_img") or ""),
        "outfit_tryon_image": str(card.get("outfit_tryon_image") or ""),
        "master_sku_id": str(card.get("master_sku_id") or ""),
        "master_spu_id": str(card.get("master_spu_id") or ""),
        "sku_ids": [str(it.get("sku_id") or "") for it in items if it.get("sku_id")],
        "spu_ids": [str(it.get("spu_id") or "") for it in items if it.get("spu_id")],
        "items": [
            {
                "sku_id": str(it.get("sku_id") or ""),
                "spu_id": str(it.get("spu_id") or ""),
                "role": str(it.get("role") or ""),
                "title": str(it.get("title") or ""),
                "price": float(it.get("price") or 0.0),
                "display_image": str(it.get("display_image") or ""),
                "tryon_image": str(it.get("tryon_image") or ""),
                "is_master": bool(it.get("is_master")),
            }
            for it in items
        ],
        "status": 1,
    }

