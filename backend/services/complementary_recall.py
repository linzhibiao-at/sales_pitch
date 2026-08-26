"""多模态互补单品召回：outfit-transformer embed_query → Milvus 近邻搜索。"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import httpx

from backend.api_debug import _debug_recall_io_enabled, log_flow
from backend.config import load_config, recall_outfit_limit
from backend.ranking.outfit_conflict import (
    build_attr_milvus_expr,
    build_scene_domain_milvus_expr,
    build_series_milvus_expr,
    check_companion_conflict,
)
from backend.recall_pathway import RecallPathway
from backend.retrieval.milvus_client import MilvusClient
from backend.retrieval.progressive_relax import (
    get_relax_config,
    run_with_progressive_relax,
)
from backend.retrieval.sku_retriever import SkuRetriever
from backend.services.synthetic_outfit import compose_outfits_from_role_recall
from backend.models import UserIntent
from backend.intent.color_series_pairing import (
    get_companion_color_series,
    resolve_anchor_color_series,
)
from backend.intent.category_l2_pairing import merge_milvus_expr
from backend.intent.role_slots import role_has_explicit_positive
from backend.retrieval.up_time_filter import build_up_time_milvus_expr

logger = logging.getLogger(__name__)

# 每个 target_role 召回的默认 top_k
DEFAULT_PER_ROLE_TOP_K = 50


def _build_role_milvus_expr(
    intent: UserIntent,
    role: str,
    *,
    anchor_cat2: str = "",
    anchor_row: dict[str, Any] | None = None,
    skip_slots: set[str] | None = None,
) -> str:
    """Build Milvus boolean expression for a single target role.

    ``skip_slots``：progressive relax 丢弃的 slot 名集合，对应 expr 片段跳过。
    gender/season/age 为硬约束，永不可跳过（不在 skip_slots 语义内）。
    """
    skip = skip_slots or set()
    parts: list[str] = [f'role == "{role}"']

    gender = intent.gender
    if gender:
        parts.append(f'array_contains(gender, "{gender}")')

    # 结构化属性粗排下推：is_intimate==false 常驻，长袖锚点排除短款下装，
    # 全身装互斥，同层叠穿互斥（见 build_attr_milvus_expr）。
    # 替代旧的 category_l2 枚举粗排（get_excluded_categories_for_milvus）。
    # 用户对该 role 有任一显式 positive 时跳过全部锚点驱动子句（用户意图优先）。
    # skip anchor_attr_must_not（progressive relax）时整体跳过。
    _bypass = role_has_explicit_positive(intent, role)
    if "anchor_attr_must_not" not in skip:
        attr_expr = build_attr_milvus_expr(
            anchor_row, role, bypass_all=_bypass,
        )
        if attr_expr:
            parts.append(attr_expr)

    # scene_domain 场景域下推：日常服×专业运动服不互搭（见 build_scene_domain_milvus_expr）。
    # 与 attr_expr 并联（不同维度，互不干扰）。
    # 用户对该 role 有任一显式 positive 时跳过锚点场景隔离——用户明确要的不应被
    # 锚点同营规则否决（如 daily 锚点 × 用户要的 golf 白色长裤）。
    # skip scene_domain（progressive relax）时跳过。
    from backend.intent.role_slots import (
        build_modeling_price_milvus_expr,
        build_role_milvus_expr_parts,
        per_role_color_series,
    )
    if _bypass:
        logger.info(
            "[complementary·explicit_bypass] role=%s 用户有显式 positive，跳过锚点场景隔离",
            role,
        )
    elif "scene_domain" not in skip:
        scene_expr = build_scene_domain_milvus_expr(anchor_row, role)
        if scene_expr:
            parts.append(scene_expr)

    # series 系列隔离下推：同系-only + 例外（见 build_series_milvus_expr）。
    # 与 scene_expr 并联（不同维度，互不干扰）。anchor 无 series 时回退 intent.series
    # （text_only 用户显式提系列），锚点有 series 时以锚点数据权威。
    # 用户对该 role 有任一显式 positive 时跳过锚点系列隔离——与 scene_domain 对称，
    # 用户明确意图优先于锚点同系假设（跨系列候选不再被预过滤提前滤掉）。
    # skip series（progressive relax）时跳过。
    if not _bypass and "series" not in skip:
        series_expr = build_series_milvus_expr(anchor_row, role, intent.series or "", bypass_all=False)
        if series_expr:
            parts.append(series_expr)

    # per-target-role 用户槽位下推：用户为该 role 明确指定的正向约束（覆盖全局默认）
    # 与否定约束（不要黑色/不要短裙等）。与锚点冲突规则并联 AND，不替换。
    # 见 backend/intent/role_slots.py 与 intent_extract.md 第十七/十五节。
    # skip_slots 透传：length_class/coverage/modeling/color_series/category/series/scene_domain
    # 的 per-role 正向/否定由 build_role_milvus_expr_parts 按 skip_slots 跳过。
    _per_role_skip = {
        "length_class", "coverage", "modeling", "color_series",
        "category", "series", "scene_domain",
    } & skip
    parts.extend(build_role_milvus_expr_parts(
        intent, role, skip_slots=_per_role_skip or None,
    ))

    # 版型 + 价格区间（global←per-role 覆盖，同义词归并），与上述 per-role 槽位并联 AND。
    # skip modeling / skip price（progressive relax，price 在链尾）。
    mp_expr = build_modeling_price_milvus_expr(
        intent, role,
        skip_modeling=("modeling" in skip), skip_price=("price" in skip),
    )
    if mp_expr:
        parts.append(mp_expr)

    # color_series 为 ARRAY 字段（commit 3167ec8 多值数组改造）：标量写法
    # ``color_series in [...]`` / ``color_series == ""`` 会让 Milvus 报 code=1100
    # “cannot be casted to Array”，整条 expr 解析失败 → 该 role 0 召回。
    # 改用 array_contains_any 命中搭配色系，array_length==0 容忍未填充色系。
    # skip color_series（progressive relax）时跳过 pairing 色系 clause。
    per_role_cs = per_role_color_series(intent, role)
    # _bypass：用户对该 role 有显式 positive 时 pairing 色系配对让路——
    # 无 per_role_cs 也不回退锚点色系配对，色系约束交给 per-role 正向过滤。
    if not per_role_cs and not _bypass and "color_series" not in skip:
        anchor_cs = resolve_anchor_color_series(anchor_row) if anchor_row else None
        if not anchor_cs and intent.color_series:
            anchor_cs = intent.color_series[0]
        # 方向化色系配对：上装锚→下装 用 top_bottom，下装锚→上装 用 bottom_top
        anchor_role = str((anchor_row or {}).get("role") or "").strip()
        companions = (
            get_companion_color_series(
                anchor_cs,
                anchor_role=anchor_role,
                companion_role=role,
            ) if anchor_cs else None
        )
        if companions:
            quoted = ", ".join(f'"{c}"' for c in companions)
            parts.append(
                f"(array_contains_any(color_series, [{quoted}]) "
                f"or array_length(color_series) == 0)"
            )

    # season 粗排下推：任何时候都不豁免，避免保暖外套等长上装锚点召回跨季节下装。
    # 跨季兼容（春夏/秋冬配对，见 models.season_compatible_set）：春锚点同时放行夏款，
    # 避免春装锚点把库里只有夏/秋款的某系列下装清零；冬装外套仍挡夏款（保护初衷）。
    from backend.models import season_compatible_set

    seasons = season_compatible_set(list(intent.season or []))
    if seasons:
        escaped = [s.replace('"', '\\"') for s in seasons]
        likes = " or ".join(f'season like "%{s}%"' for s in escaped)
        parts.append(f"({likes})")

    # age 粗排下推：童装锚点只召回同段或通码，排除异段（如中大童底装召回小童上装）
    # 与成人款（age 为空）。锚点 age 优先（被互补单品的权威段），缺失时回退 intent.age。
    anchor_age = ""
    if anchor_row:
        anchor_age = str(anchor_row.get("age") or "").strip()
    if not anchor_age and getattr(intent, "age", None):
        anchor_age = str(intent.age).strip()
    if anchor_age == "通码":
        # 通码锚点覆盖全段，可与任一童装段搭配，但仍排除成人款。
        parts.append('age != ""')
    elif anchor_age:
        parts.append(f'age in ["{anchor_age}", "通码"]')

    # 全局上架时间下限：up_time >= config.recommend.up_time_since（与 ES 路、build_catalog 对齐）
    # 禁用（配置留空）或 skip up_time（progressive relax 链尾）时跳过。
    if "up_time" not in skip:
        _up_time_expr = build_up_time_milvus_expr()
        if _up_time_expr:
            parts.append(_up_time_expr)

    return " and ".join(parts)


def _get_complementary_config() -> dict[str, Any]:
    cfg = load_config()
    rec = cfg.get("recommend") or {}
    return rec.get("complementary_model") or {}


def _embed_query_via_serve(
    items: list[dict[str, str]],
    *,
    service_url: str,
    timeout: int = 5,
) -> list[float] | None:
    """Call outfit-transformer serve /embed_query to get 128-dim complementary vector.

    items: [{"image_url": "...", "description": "..."}]
    """
    try:
        resp = httpx.post(
            f"{service_url.rstrip('/')}/embed_query",
            json={"items": items},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json().get("embedding")
    except Exception:
        logger.exception("complementary_model embed_query failed")
        return None


def _search_one_role(
    milvus: MilvusClient,
    sku_r: SkuRetriever,
    embedding: list[float],
    role: str,
    intent: UserIntent,
    anchor_id: str,
    anchor_cat2: str,
    top_k: int,
    anchor_row: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Search Milvus for a single role and return enriched SKU rows.

    0 命中时按 progressive relax 优先级逐个丢弃 soft slot 直到命中数达标。
    硬约束 gender/season/age 不在 relax_priority 中，循环不会触及。
    """
    relax_enabled, relax_priority, relax_min_hits = get_relax_config()

    def _search_fn(dropped: set[str]) -> list[dict[str, Any]]:
        expr = _build_role_milvus_expr(
            intent, role, anchor_cat2=anchor_cat2, anchor_row=anchor_row,
            skip_slots=dropped or None,
        )
        if not dropped:
            logger.info(
                "[complementary·milvus_expr] role=%s anchor_cat2=%s expr=%s",
                role, anchor_cat2, expr,
            )
        pairs = milvus.search_sku_complementary_vectors(embedding, top_k, expr=expr)
        results: list[dict[str, Any]] = []
        for sid, dist in pairs:
            if sid == anchor_id:
                continue
            sim = milvus.hit_to_similarity(float(dist))
            row = sku_r.get_sku(sid)
            if not row:
                continue
            # 安全网精排：expr 粗排已下推单侧属性规则，此处兜底成对冲突规则
            if anchor_row and check_companion_conflict(
                anchor_row, row,
                bypass_all=role_has_explicit_positive(intent, role),
            ):
                logger.info(
                    "[complementary·冲突过滤] anchor=%s 与 sku=%s "
                    "category_l2=%s title=%s 属性冲突，跳过",
                    anchor_id, sid, row.get("category_l2"), row.get("title"),
                )
                continue
            c = dict(row)
            c["_complementary_sim"] = float(sim)
            results.append(c)
        return results

    if relax_enabled:
        results, dropped_list = run_with_progressive_relax(
            _search_fn, relax_priority, relax_min_hits,
        )
        if dropped_list:
            logger.info(
                "[complementary·progressive_relax] role=%s dropped=%s results=%d",
                role, dropped_list, len(results),
            )
    else:
        results = _search_fn(set())
    return results


def recall_complementary_model_skus(
    anchor_row: dict[str, Any],
    sku_r: SkuRetriever,
    milvus: MilvusClient,
    intent: UserIntent,
    *,
    top_k: int | None = None,
    trace_id: str | None = None,
) -> list[dict[str, Any]]:
    """Anchor SKU → embed_query → per-role parallel Milvus search.

    Returns list of SKU row dicts with ``_complementary_sim`` field.
    """
    cm_cfg = _get_complementary_config()
    service_url = cm_cfg.get("service_url") or ""
    if not service_url:
        logger.info("[complementary_model] service_url not configured, skipping")
        return []

    timeout = int(cm_cfg.get("timeout") or 5)
    per_role_k = top_k or int(cm_cfg.get("top_k_per_role") or DEFAULT_PER_ROLE_TOP_K)

    # Determine target roles (exclude anchor's own role)
    anchor_role = str(anchor_row.get("role") or "").strip()
    target_roles = [
        str(r).strip()
        for r in (intent.target_roles or [])
        if str(r).strip() and str(r).strip() != anchor_role
    ]
    if not target_roles:
        logger.info("[complementary_model] no target_roles after excluding anchor role=%s, skipping", anchor_role)
        return []

    # Build item payload from anchor
    image_url = str(anchor_row.get("display_image") or "").strip()
    if not image_url:
        images = anchor_row.get("images") or []
        if images:
            image_url = str(images[0] if isinstance(images[0], str) else images[0].get("url", ""))
    description = str(anchor_row.get("title") or "").strip()
    if not image_url:
        logger.info("[complementary_model] anchor has no image, skipping")
        return []

    items_payload = [{"image_url": image_url, "description": description}]

    # Call serve embed_query (one call, reuse vector for all roles)
    embedding = _embed_query_via_serve(
        items_payload, service_url=service_url, timeout=timeout,
    )
    if not embedding:
        return []

    # Parallel Milvus search per target role
    anchor_id = str(anchor_row.get("sku_id") or "")
    anchor_cat2 = str(anchor_row.get("category_l2") or "").strip()
    if not anchor_cat2 and intent.category:
        anchor_cat2 = str(intent.category[0]).strip()
    all_results: list[dict[str, Any]] = []
    role_details: dict[str, int] = {}

    with ThreadPoolExecutor(max_workers=len(target_roles)) as pool:
        future_to_role = {
            pool.submit(
                _search_one_role, milvus, sku_r, embedding,
                role, intent, anchor_id, anchor_cat2, per_role_k,
                anchor_row,
            ): role
            for role in target_roles
        }
        for future in as_completed(future_to_role):
            role = future_to_role[future]
            try:
                role_skus = future.result()
            except Exception:
                logger.exception("[complementary_model] search failed for role=%s", role)
                role_skus = []
            role_details[role] = len(role_skus)
            all_results.extend(role_skus)

    if _debug_recall_io_enabled():
        log_flow(
            "complementary_model_recall",
            {
                "trace_id": trace_id,
                "anchor_sku_id": anchor_id,
                "embed_dim": len(embedding),
                "target_roles": target_roles,
                "per_role_top_k": per_role_k,
                "role_hits": role_details,
                "total_results": len(all_results),
                "top_skus": [
                    {"sku_id": r.get("sku_id"), "sim": round(r["_complementary_sim"], 4)}
                    for r in all_results[:5]
                ],
            },
        )

    logger.info(
        "[complementary_model] anchor=%s, roles=%s, per_role_k=%d, total=%d, skus:\n%s",
        anchor_id, role_details, per_role_k, len(all_results),
        "\n".join(
            f"  {r.get('sku_id')}  sim={r['_complementary_sim']:.4f}  role={r.get('role','')}  cat2={r.get('category_l2','')}"
            for r in all_results
        ),
    )
    return all_results


def recall_complementary_skus(
    sku_r: SkuRetriever,
    milvus: MilvusClient,
    intent: UserIntent,
    compose_anchor_row: dict[str, Any] | None,
    *,
    trace_id: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """通路4·召回阶段：多模态互补模型召回 SKU → 按 role 分组（去重/上限）。

    返回 ``{role: [sku_row, ...]}``，每行带 ``_complementary_sim``。
    组合阶段交给全局池（global 模式）或直接拼套（per_channel 模式）。
    """
    if not compose_anchor_row:
        return {}

    skus = recall_complementary_model_skus(
        compose_anchor_row,
        sku_r,
        milvus,
        intent,
        trace_id=trace_id,
    )
    if not skus:
        return {}

    by_role: dict[str, list[dict[str, Any]]] = {}
    for s in skus:
        role = str(s.get("role") or "").strip()
        if role:
            by_role.setdefault(role, []).append(s)

    cfg = load_config()
    rec = cfg.get("recommend") or {}
    per_role = int(rec.get("default_sku_per_role") or 3)
    for role in by_role:
        by_role[role] = by_role[role][:per_role]
    return by_role


def recall_complementary_composed_outfits(
    sku_r: SkuRetriever,
    milvus: MilvusClient,
    intent: UserIntent,
    compose_anchor_row: dict[str, Any] | None,
    *,
    trace_id: str | None = None,
) -> list[dict[str, Any]]:
    """通路4·per_channel 模式：互补模型召回 SKU（去重）→ 直接拼套。

    global 模式下不调用本函数，改由 multi_path_recall 聚合进全局池统一拼套。
    """
    if not compose_anchor_row:
        return []

    by_role = recall_complementary_skus(
        sku_r, milvus, intent, compose_anchor_row, trace_id=trace_id,
    )
    if not by_role:
        return []

    cfg = load_config()
    rec = cfg.get("recommend") or {}
    per_role = int(rec.get("default_sku_per_role") or 3)
    max_outfits = recall_outfit_limit(cfg)

    anchor_for_compose = dict(compose_anchor_row)
    anchor_for_compose["_is_image_input_anchor"] = True

    composed = compose_outfits_from_role_recall(
        anchor_for_compose,
        by_role,
        max_outfits=max_outfits,
        picks_per_role=per_role,
        source="complementary_model_compose",
    )
    for o in composed:
        o["source"] = "complementary_model_compose"
        o["_recall_path"] = RecallPathway.OUTFIT_COMPLEMENTARY_MODEL.value
    return composed
