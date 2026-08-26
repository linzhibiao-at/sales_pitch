"""全局候选单品池：聚合各路 per-role 召回 SKU，RRF 融合多路分数。

重构后的搭配召回流程：各路只召回 per-role 单品（带本路分数）→ 进全局池
聚合 → 用 Reciprocal Rank Fusion (RRF) 把「同一 SKU 被多路召回」降为单一
``_pool_score`` → 再交给 ``_dedupe_role_recall_skus`` 做 per-role 去重/上限
→ 喂给 ``compose_outfits_from_role_recall`` 统一组合一次。

这样三路召回的 SKU 互补：某路缺某 role 时，其他路的同 role 单品可补上，
避免单路召回不全导致好单品被埋进残缺搭配（详见 plan steady-dancing-puddle）。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# RRF 常量，与 outfit_recall.merge_and_dedupe_outfits 的 outfit 级 RRF 口径一致。
RRF_K = 60

# 各召回通路 → 其打在 SKU 行上的分数字段。
# 与 outfit_recall 的 _outfit_*_score / complementary_recall._complementary_sim 对齐。
CHANNEL_SCORE_FIELD: dict[str, str] = {
    "text_vector": "_text_vector_sim",
    "query2es": "_es_score",
    "complementary_model": "_complementary_sim",
}


def _channel_score(row: dict[str, Any], pathway: str) -> float:
    field = CHANNEL_SCORE_FIELD.get(pathway, "")
    if not field:
        return 0.0
    v = row.get(field)
    return float(v) if v is not None else 0.0


def build_candidate_pool(
    per_channel_by_role: dict[str, dict[str, list[dict[str, Any]]]],
    anchor_sku_id: str,
    *,
    k: int = RRF_K,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """聚合各路 per-role 召回 SKU，RRF 融合多路分数。

    Args:
        per_channel_by_role: ``{pathway: {role: [sku_row, ...]}}``。每个 sku_row 需带
            该 pathway 对应的分数字段（见 ``CHANNEL_SCORE_FIELD``）。
        anchor_sku_id: 锚点 SKU，从池中剔除（搭配锚点由 compose 阶段注入）。
        k: RRF 平滑常数。

    Returns:
        (by_role_rows, pool_debug):
        - ``by_role_rows``: ``{role: [merged_row, ...]}``，每个 merged_row 携带
          ``_channel_scores: {pathway: score}``、``_pool_score: float``、
          ``_contributing_pathways: list[str]``，以及代表 SKU 行的全部原始字段。
        - ``pool_debug``: per-role / per-channel 计数与多路命中统计，供 trace。
    """
    aid = (anchor_sku_id or "").strip()

    # 收集所有出现过的 role
    roles: set[str] = set()
    for role_map in per_channel_by_role.values():
        roles.update(role_map.keys())

    by_role: dict[str, list[dict[str, Any]]] = {}
    role_debug: dict[str, Any] = {}
    multi_hit_total = 0

    for role in roles:
        # sid -> {"row": 代表行, "scores": {pathway: score}, "ranks": {pathway: rank}}
        sid_info: dict[str, dict[str, Any]] = {}

        for pathway, role_map in per_channel_by_role.items():
            rows = role_map.get(role) or []
            if not rows:
                continue
            # 该路内按本路分数降序定 rank（稳定：分数相同按原顺序）
            ranked = sorted(
                rows,
                key=lambda r: _channel_score(r, pathway),
                reverse=True,
            )
            for rank, row in enumerate(ranked, start=1):
                sid = str(row.get("sku_id") or "")
                if not sid or sid == aid:
                    continue
                score = _channel_score(row, pathway)
                info = sid_info.get(sid)
                if info is None:
                    # 代表行取首次出现的那路 row；后续路只叠加分数。
                    sid_info[sid] = {
                        "row": dict(row),
                        "scores": {},
                        "ranks": {},
                    }
                info = sid_info[sid]
                # 同一通路内 sid 不会重复（各路已 dedup），直接覆盖
                info["scores"][pathway] = score
                info["ranks"][pathway] = rank

        merged_rows: list[dict[str, Any]] = []
        multi_hit_role = 0
        for sid, info in sid_info.items():
            ranks = info["ranks"]
            pool_score = sum(1.0 / (k + r) for r in ranks.values())
            row = info["row"]
            # 把各路原始分数也保留在 row 上（供下游 _sku_score / 归因读取），
            # 但组合阶段排序统一用 _pool_score（见 synthetic_outfit._sku_score）。
            for pathway, score in info["scores"].items():
                field = CHANNEL_SCORE_FIELD.get(pathway)
                if field and row.get(field) is None:
                    row[field] = score
            row["_channel_scores"] = info["scores"]
            row["_pool_score"] = pool_score
            row["_contributing_pathways"] = sorted(ranks.keys())
            if len(ranks) > 1:
                multi_hit_role += 1
                multi_hit_total += 1
            merged_rows.append(row)

        # 池内按融合分降序
        merged_rows.sort(
            key=lambda r: float(r.get("_pool_score") or 0.0),
            reverse=True,
        )
        if merged_rows:
            by_role[role] = merged_rows
        role_debug[role] = {
            "total": len(merged_rows),
            "multi_channel_hits": multi_hit_role,
            "by_channel": {
                pw: len((per_channel_by_role.get(pw) or {}).get(role) or [])
                for pw in per_channel_by_role
                if (per_channel_by_role.get(pw) or {}).get(role)
            },
        }

    pool_debug = {
        "roles": role_debug,
        "multi_channel_hits_total": multi_hit_total,
        "channels": list(per_channel_by_role.keys()),
    }
    logger.info(
        "[candidate_pool] roles=%s, multi_channel_hits=%d, per_role=%s",
        sorted(by_role.keys()), multi_hit_total,
        {r: len(v) for r, v in by_role.items()},
    )
    return by_role, pool_debug
