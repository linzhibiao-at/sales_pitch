"""按 target_role 生成可执行 ES query：LLM + 规则降级。"""

from __future__ import annotations

import logging
from typing import Any

from backend.config import get_elasticsearch_index
from backend.intent.category_l2_pairing import (
    build_category_l2_es_filter,
    filter_companions_for_target_role,
    get_pairing_list_mode,
)
from backend.intent.color_series_pairing import (
    build_color_series_es_filter,
)
from backend.llm_client import extract_es_sku_query_json
from backend.models import UserIntent, normalize_age, normalize_gender, normalize_season
from backend.ranking.outfit_conflict import (
    build_attr_es_filter,
    build_scene_domain_es_filter,
    build_series_es_filter,
)
from backend.query_keywords import (
    _shared_intent_tokens,
    build_query_for_target_role,
)
from backend.retrieval.up_time_filter import build_up_time_es_filter

logger = logging.getLogger(__name__)

_SKU_TEXT_FIELDS = [
    "title^2",
    "search_text",
    "search_keywords",
]


def _user_message_with_index(
    user_text: str,
    index_name: str,
    role: str,
    *,
    anchor_hint: str = "",
) -> str:
    text = (user_text or "").strip()[:4000]
    index = (index_name or "").strip() or get_elasticsearch_index("skus")
    role_s = (role or "").strip()
    lines = [f"目标索引：{index}", f"检索词：{text}"]
    if role_s:
        lines.insert(1, f"目标 role（须在 filter 中用 term.role）：{role_s}")
    if anchor_hint:
        lines.append(f"搭配锚点：{anchor_hint}")
    return "\n".join(lines)


def _build_fallback_q(intent: UserIntent, role: str, *, anchor_title: str = "") -> str:
    shared = _shared_intent_tokens(intent)
    role_q = build_query_for_target_role(role, shared)
    parts: list[str] = []
    raw = (intent.text or "").strip()
    if raw:
        parts.append(raw)
    if anchor_title:
        from backend.query_keywords import _role_zh
        parts.append(f"搭配{anchor_title}的{_role_zh(role)}")
    if role_q and role_q not in parts:
        parts.append(role_q)
    return "，".join(p for p in parts if p) or role_q or raw


def _multi_must(text: str) -> dict[str, Any]:
    if not text:
        return {"match_all": {}}
    return {
        "multi_match": {
            "query": text,
            "fields": _SKU_TEXT_FIELDS,
            "type": "best_fields",
            "operator": "or",
            "lenient": True,
        },
    }


def _unwrap_es_query(parsed: dict[str, Any]) -> dict[str, Any]:
    if not parsed:
        return {}
    inner = parsed.get("query")
    if isinstance(inner, dict):
        return inner
    if any(
        key in parsed
        for key in ("bool", "multi_match", "match_all", "term", "match")
    ):
        return parsed
    return {}


def _strip_category_l2(node: Any) -> Any:
    if isinstance(node, dict):
        term = node.get("term")
        if isinstance(term, dict) and "category_l2" in term:
            return None
        out: dict[str, Any] = {}
        for key, val in node.items():
            cleaned = _strip_category_l2(val)
            if cleaned is None:
                continue
            if isinstance(cleaned, list):
                cleaned = [x for x in cleaned if x is not None]
                if key in ("filter", "must", "should", "must_not") and not cleaned:
                    continue
            out[key] = cleaned
        return out
    if isinstance(node, list):
        items = [_strip_category_l2(x) for x in node]
        return [x for x in items if x is not None]
    return node


def _looks_like_legacy_extraction(parsed: dict[str, Any]) -> bool:
    legacy_keys = (
        "gender",
        "category_l1",
        "keywords",
        "role",
        "season",
        "price_max",
    )
    return any(k in parsed for k in legacy_keys)


def _text_for_must(extraction: dict[str, Any], fallback_q: str) -> str:
    if not extraction:
        return (fallback_q or "").strip()
    if "keywords" in extraction:
        kw = extraction.get("keywords")
        if kw is None:
            return ""
        if isinstance(kw, list):
            return " ".join(str(x).strip() for x in kw if str(x).strip())
        s = str(kw).strip()
        return "" if s.lower() in ("null", "none") else s
    norm = extraction.get("normalized_text")
    if norm is not None:
        s = str(norm).strip()
        if s and s.lower() not in ("null", "none"):
            return s
    return ""


def _append_term(filters: list[dict[str, Any]], field: str, value: object) -> None:
    if value is None:
        return
    s = str(value).strip()
    if not s:
        return
    filters.append({"term": {field: s}})


_CHILDREN_GENDER_EXPANSION = ["儿童", "男童", "女童"]


def _append_gender_filter(filters: list[dict[str, Any]], gender: str | None) -> None:
    """gender=儿童 时扩展为 terms 查询，同时匹配 男童/女童。"""
    if gender is None:
        return
    s = str(gender).strip()
    if not s:
        return
    if s == "儿童":
        filters.append({"terms": {"gender": _CHILDREN_GENDER_EXPANSION}})
    else:
        filters.append({"term": {"gender": s}})


def _append_age_filter(filters: list[dict[str, Any]], age: str | None) -> None:
    """年龄段过滤：指定 小童/中大童/婴幼童 时同时命中通码（同款覆盖全段）。

    空值（成人款或未分段）不命中，从而把成人款排除在童装年龄段查询之外。
    通码作为查询值时仅精确匹配通码。未指定 age 时不加 filter。
    """
    if age is None:
        return
    s = str(age).strip()
    if not s:
        return
    if s == "通码":
        filters.append({"term": {"age": "通码"}})
    else:
        filters.append({"terms": {"age": [s, "通码"]}})


def _append_season_sku(
    filters: list[dict[str, Any]],
    seasons: object,
    *,
    expand_compat: bool = False,
) -> None:
    """季节粗排：wildcard 子串匹配 ``*春*``（season 字段为标量串，存 "春"/"春夏" 等）。

    ``expand_compat``：按跨季兼容矩阵（春夏/秋冬配对，见
    ``models.season_compatible_set``）展开 want 集——仅互补召回 intent 路开，
    让春锚点同时放行夏款。SKU 直接检索（``_build_sku_query_from_extraction``）
    保持严格精确匹配，不过度放宽。
    """
    if not isinstance(seasons, list) or not seasons:
        return
    if expand_compat:
        from backend.models import season_compatible_set

        seasons = season_compatible_set(seasons)
    should: list[dict[str, Any]] = []
    for s in seasons:
        token = str(s).strip()
        if not token:
            continue
        should.append({"wildcard": {"season": f"*{token}*"}})
    if not should:
        return
    if len(should) == 1:
        filters.append(should[0])
    else:
        filters.append(
            {
                "bool": {
                    "should": should,
                    "minimum_should_match": 1,
                },
            },
        )


def _build_sku_query_from_extraction(
    extraction: dict[str, Any],
    fallback_q: str,
) -> dict[str, Any]:
    filters: list[dict[str, Any]] = []
    _append_gender_filter(filters, normalize_gender(extraction.get("gender")))
    _append_age_filter(filters, normalize_age(extraction.get("age")))
    _append_term(filters, "category_l1", extraction.get("category_l1"))
    _append_term(filters, "role", extraction.get("role"))
    _append_term(filters, "series", extraction.get("series"))
    _append_term(filters, "sku_id", extraction.get("sku_id"))
    _append_term(filters, "spu_id", extraction.get("spu_id"))
    _append_season_sku(filters, normalize_season(extraction.get("season")))
    pm = extraction.get("price_max")
    if pm is not None:
        try:
            v = float(pm)
            filters.append({"range": {"price": {"lte": v}}})
        except (TypeError, ValueError):
            pass
    must = _multi_must(_text_for_must(extraction, fallback_q))
    if not filters:
        return must
    if must.get("match_all") is not None:
        return {"bool": {"filter": filters}}
    return {"bool": {"must": [must], "filter": filters}}


def fallback_simple_query(q: str) -> dict[str, Any]:
    return {"match_all": {}}


def coerce_llm_es_query(
    parsed: dict[str, Any],
    fallback_q: str,
) -> dict[str, Any]:
    query = _unwrap_es_query(parsed)
    if not query:
        if _looks_like_legacy_extraction(parsed):
            return _build_sku_query_from_extraction(parsed, fallback_q)
        return fallback_simple_query(fallback_q)
    cleaned = _strip_category_l2(query)
    if isinstance(cleaned, dict) and cleaned:
        return cleaned
    return fallback_simple_query(fallback_q)


def _has_role_filter(query: dict[str, Any], role: str) -> bool:
    role_s = (role or "").strip()
    if not role_s:
        return True

    def walk(node: Any) -> bool:
        if isinstance(node, dict):
            term = node.get("term")
            if isinstance(term, dict) and term.get("role") == role_s:
                return True
            return any(walk(v) for v in node.values())
        if isinstance(node, list):
            return any(walk(x) for x in node)
        return False

    return walk(query)


def _ensure_role_filter(query: dict[str, Any], role: str) -> dict[str, Any]:
    role_s = (role or "").strip()
    if not role_s or _has_role_filter(query, role_s):
        return query
    term = {"term": {"role": role_s}}
    if query.get("match_all") is not None:
        return {"bool": {"filter": [term]}}
    if "bool" in query and isinstance(query["bool"], dict):
        b = dict(query["bool"])
        flt = list(b.get("filter") or [])
        flt.append(term)
        b["filter"] = flt
        return {"bool": b}
    return {"bool": {"must": [query], "filter": [term]}}


def _build_intent_filters(
    intent: UserIntent,
    *,
    skip_color_series: bool = False,
    skip_budget: bool = False,
    skip_modeling: bool = False,
) -> list[dict[str, Any]]:
    """从 intent 提取 gender / age / season / budget / color_series / modeling 构建 ES filter 子句。

    ``skip_budget`` / ``skip_modeling``：query2es per-role 通路会把价格与版型改为
    按角色 effective 注入（避免全局与 per-role 同时 AND 致空），此时跳过意图级发射。
    """
    filters: list[dict[str, Any]] = []
    _append_gender_filter(filters, intent.gender)
    _append_age_filter(filters, intent.age)
    _append_season_sku(filters, intent.season, expand_compat=True)
    if not skip_budget:
        rng: dict[str, Any] = {}
        if intent.budget_min is not None and intent.budget_min > 0:
            rng["gte"] = intent.budget_min
        if intent.budget_max is not None and intent.budget_max > 0:
            rng["lte"] = intent.budget_max
        if rng:
            filters.append({"range": {"price": rng}})
    if not skip_modeling and intent.modeling:
        from backend.intent.sku_attributes import expand_modeling
        expanded = expand_modeling(intent.modeling)
        if expanded:
            filters.append({"terms": {"modeling": expanded}})
    # 色系过滤（多值 OR）—— 搭配召回时由 pairing filter 替代，跳过
    if not skip_color_series:
        cs = [str(s).strip() for s in (intent.color_series or []) if str(s).strip()]
        if cs:
            if len(cs) == 1:
                filters.append({"term": {"color_series": cs[0]}})
            else:
                filters.append({"terms": {"color_series": cs}})
    return filters


def _build_intent_should(intent: UserIntent) -> list[dict[str, Any]]:
    """从 intent 提取 style/occasion/color/season 构建 ES should 加分子句。"""
    tags: list[str] = []
    for t in intent.style_tags or []:
        s = str(t).strip()
        if s:
            tags.append(s)
    for t in intent.occasion_tags or []:
        s = str(t).strip()
        if s:
            tags.append(s)
    should: list[dict[str, Any]] = []
    for tag in tags:
        should.append({
            "multi_match": {
                "query": tag,
                "fields": _SKU_TEXT_FIELDS,
                "type": "best_fields",
                "lenient": True,
            },
        })
    # season 精确匹配加分：season="夏" 优先于 season="春夏"
    for s in intent.season or []:
        token = str(s).strip()
        if not token:
            continue
        should.append({"term": {"season": {"value": token, "boost": 2}}})
    return should


def _merge_intent_clauses(
    query: dict[str, Any],
    filters: list[dict[str, Any]],
    should: list[dict[str, Any]],
) -> dict[str, Any]:
    """将 intent 级别的 filter 和 should 合并到已有 ES query 中。"""
    if "bool" in query and isinstance(query["bool"], dict):
        b = dict(query["bool"])
        if filters:
            flt = list(b.get("filter") or [])
            flt.extend(filters)
            b["filter"] = flt
        if should:
            s = list(b.get("should") or [])
            s.extend(should)
            b["should"] = s
        return {"bool": b}
    # 裸 multi_match / match_all → 包进 bool
    parts: dict[str, Any] = {}
    if query.get("match_all") is None:
        parts["must"] = [query]
    if filters:
        parts["filter"] = filters
    if should:
        parts["should"] = should
    return {"bool": parts}


def _merge_es_filters(
    query: dict[str, Any],
    extra_filters: list[dict[str, Any]],
) -> dict[str, Any]:
    """将额外 filter 子句合并进 ES query。"""
    if not extra_filters:
        return query
    if "bool" in query and isinstance(query["bool"], dict):
        b = dict(query["bool"])
        flt = list(b.get("filter") or [])
        flt.extend(extra_filters)
        b["filter"] = flt
        return {"bool": b}
    if query.get("match_all") is not None:
        return {"bool": {"filter": extra_filters}}
    return {"bool": {"must": [query], "filter": extra_filters}}


def _merge_es_must_not(
    query: dict[str, Any],
    must_not_clauses: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """将 must_not 子句合并进 ES query 的 bool.must_not。"""
    if not must_not_clauses:
        return query
    if "bool" in query and isinstance(query["bool"], dict):
        b = dict(query["bool"])
        mn = list(b.get("must_not") or [])
        mn.extend(must_not_clauses)
        b["must_not"] = mn
        return {"bool": b}
    if query.get("match_all") is not None:
        return {"bool": {"must_not": must_not_clauses}}
    return {"bool": {"must": [query], "must_not": must_not_clauses}}


def resolve_es_query_for_role(
    intent: UserIntent,
    role: str,
    *,
    index_name: str | None = None,
    llm_enabled: bool = True,
    image_base64: str | None = None,
    model_override: str | None = None,
    anchor_context: dict[str, str] | None = None,
    anchor_row: dict[str, Any] | None = None,
    allowed_companion_cat2: list[str] | None = None,
    allowed_companion_color_series: list[str] | None = None,
    skip_color_series: bool = False,
    skip_modeling: bool = False,
    skip_length_class: bool = False,
    skip_coverage: bool = False,
    skip_series: bool = False,
    skip_scene_domain: bool = False,
    skip_category_l2: bool = False,
    skip_anchor_attr_must_not: bool = False,
    skip_up_time: bool = False,
    skip_price: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """为单个 target_role 生成 ES query 子句与调试元数据。

    ``anchor_row``（可选）用于驱动结构化属性单侧排除规则下推到 ES ``must_not``
    （见 ``build_attr_es_filter``）与 scene_domain 正向隔离下推到 ES ``filter``
    （见 ``build_scene_domain_es_filter``）。与 Milvus 路的
    ``build_attr_milvus_expr`` / ``build_scene_domain_milvus_expr`` 对称，把
    post-filter 提前到搜索条件。
    """
    index_name = (index_name or "").strip() or get_elasticsearch_index("skus")
    anchor_title = (anchor_context or {}).get("title", "")
    anchor_cat2 = (anchor_context or {}).get("category_l2", "")
    fallback_q = _build_fallback_q(intent, role, anchor_title=anchor_title)
    source = "fallback"
    parsed: dict[str, Any] = {}

    if llm_enabled and (fallback_q or (image_base64 or "").strip()):
        anchor_hint = ""
        if anchor_title:
            anchor_hint = f"{anchor_title}（{anchor_cat2}）" if anchor_cat2 else anchor_title
        user_msg = _user_message_with_index(
            fallback_q, index_name, role, anchor_hint=anchor_hint,
        )
        try:
            parsed = extract_es_sku_query_json(
                user_msg,
                image_base64=image_base64,
                model_override=model_override,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("es_intent LLM failed role=%s: %s", role, e)
            parsed = {}
        if parsed:
            source = "llm"

    es_query = coerce_llm_es_query(parsed, fallback_q)
    es_query = _ensure_role_filter(es_query, role)
    # 全局上架时间下限：up_time >= config.recommend.up_time_since（与 Milvus expr、build_catalog 对齐）
    # 禁用（配置留空）或 skip_up_time（progressive relax 链尾放宽）时跳过。
    if not skip_up_time:
        _up_time_filter = build_up_time_es_filter()
        if _up_time_filter:
            es_query = _merge_es_filters(es_query, [_up_time_filter])

    # 从 intent 补充结构化 filter 和 should
    # 当有色系搭配 filter 时，跳过 intent 级别的 color_series filter（避免冲突）
    # 当 skip_color_series=True（色系 pairing 关闭）时，也跳过
    # per-target-role 覆盖（见 role_slots.py / intent_extract.md 第十七/十五节）：
    # 用户为该 role 有任一显式 positive 时，覆盖 pairing cs_filter 与锚点结构化/
    # 场景隔离——用户明确意图优先，per-role 正向覆盖 + 否定下推到 ES。
    from backend.intent.role_slots import (
        build_role_es_must_not,
        build_role_es_positive_filters,
        effective_role_budget,
        effective_role_slots,
        per_role_category,
        per_role_color_series,
        role_has_explicit_positive,
    )
    _bypass = role_has_explicit_positive(intent, role)
    if _bypass:
        logger.info(
            "[es_intent·explicit_bypass] role=%s 用户有显式 positive，跳过锚点场景/结构/系列/pairing 隔离",
            role,
        )
    per_role_cs = per_role_color_series(intent, role)
    if skip_color_series:
        # progressive relax：色系（pairing + per-role 覆盖）整体让路
        effective_cs = None
    elif per_role_cs:
        effective_cs = per_role_cs
        logger.info(
            "[es_intent·cs_override] role=%s 用户显式 color_series=%s，覆盖 pairing cs_filter",
            role, per_role_cs,
        )
    elif not _bypass:
        effective_cs = allowed_companion_color_series
    else:
        # _bypass：用户对该 role 有显式 positive，pairing cs_filter 让路——
        # 无 per_role_cs 时不回退 pairing，色系约束完全交给 per-role 正向过滤。
        effective_cs = None
    per_role_cat2 = per_role_category(intent, role)
    if per_role_cat2:
        logger.info(
            "[es_intent·cat2_override] role=%s 用户显式 category=%s，覆盖 pairing cat2_filter",
            role, per_role_cat2,
        )

    # 价格/版型改为按角色 effective 注入（global←per-role 覆盖），避免意图级与
    # per-role 同时 AND 致空，故此处跳过意图级 budget/modeling 发射。
    intent_filters = _build_intent_filters(
        intent,
        skip_color_series=skip_color_series or bool(effective_cs),
        skip_budget=True,
        skip_modeling=True,
    )
    intent_should = _build_intent_should(intent)
    if intent_filters or intent_should:
        es_query = _merge_intent_clauses(es_query, intent_filters, intent_should)

    # per-role category 覆盖 pairing 互补中类白名单：用户显式指定品类时以其为准，
    # 否则沿用 pairing 互补列表（按 target_role 收窄）。
    # _bypass 时 pairing 让路：无 per_role_cat2 则不回退 pairing，中类约束完全交给
    # per-role 正向过滤 + role 守卫（类型正确性由 role== 保证，放宽的是中类互补白名单）。
    if per_role_cat2:
        effective_cat2: list[str] | None = per_role_cat2
    elif skip_category_l2:
        effective_cat2 = None
    elif not _bypass:
        effective_cat2 = filter_companions_for_target_role(
            allowed_companion_cat2,
            role,
        )
    else:
        effective_cat2 = None
    cat2_filter = build_category_l2_es_filter(effective_cat2 or [])
    if cat2_filter:
        es_query = _merge_es_filters(es_query, [cat2_filter])

    cs_filter = build_color_series_es_filter(effective_cs)
    if cs_filter:
        es_query = _merge_es_filters(es_query, [cs_filter])

    # 结构化属性单侧排除规则下推到 ES must_not
    # （镜像 Milvus 路 build_attr_milvus_expr）。
    # 把 is_intimate/length_class/coverage/layer 的 post-filter 提前到搜索条件，
    # 减少 ES 命中后再 Python 过滤的开销。成对规则仍由 check_companion_conflict
    # 安全网兜底。skip_anchor_attr_must_not（progressive relax）时整体跳过。
    if not skip_anchor_attr_must_not:
        attr_must_not = build_attr_es_filter(
            anchor_row, role, bypass_all=_bypass,
        )
        if attr_must_not:
            es_query = _merge_es_must_not(es_query, attr_must_not["must_not"])

    # scene_domain 正向隔离下推到 ES filter（镜像 Milvus 路
    # build_scene_domain_milvus_expr）：只召回与锚点同营的域，异营域不进 ES。
    # 与 cat2_filter/cs_filter 同走 bool.filter。
    # 用户对该 role 有任一显式 positive 时跳过锚点场景隔离，per-role scene_domain
    #（若有）由下方 build_role_es_positive_filters 推入。
    # skip_scene_domain（progressive relax）时跳过锚点场景隔离。
    if not _bypass and not skip_scene_domain:
        scene_filter = build_scene_domain_es_filter(anchor_row, role)
        if scene_filter:
            es_query = _merge_es_filters(es_query, [scene_filter])

    # series 系列隔离下推到 ES filter（镜像 Milvus 路 build_series_milvus_expr）：
    # 只召回与锚点同系（或无系列）的候选，异系列不进 ES。与 scene_filter 同走 bool.filter。
    # anchor_row 无 series 时回退 intent.series（text_only 显式系列请求）。
    # 用户对该 role 有任一显式 positive 时跳过锚点系列隔离——与 scene_domain 对称，
    # 跨系列候选不再被预过滤提前滤掉（用户明确意图优先于锚点同系假设）。
    # skip_series（progressive relax）时跳过锚点系列隔离。
    if not _bypass and not skip_series:
        series_filter = build_series_es_filter(anchor_row, role, intent.series or "")
        if series_filter:
            es_query = _merge_es_filters(es_query, [series_filter])

    # per-target-role 正向覆盖（length_class/coverage/scene_domain）与否定
    # （不要黑色/不要短裙等）下推到 ES。与 pairing cat2/cs filter 并联 AND。
    # color_series/category 已分别由上方 cs_filter / cat2_filter（含 per-role
    # 覆盖）统一处理；modeling 由下方 effective 注入（同义词归并），此处均排除，
    # 避免同一条件被 AND 两次。
    # progressive relax：skip_length_class/skip_coverage/skip_scene_domain/skip_series
    # 通过 exclude_slots 让对应 per-role 正向覆盖也让路。
    _pos_excl: set[str] = {"color_series", "category", "modeling"}
    if skip_length_class:
        _pos_excl.add("length_class")
    if skip_coverage:
        _pos_excl.add("coverage")
    if skip_scene_domain:
        _pos_excl.add("scene_domain")
    if skip_series:
        _pos_excl.add("series")
    role_pos_filters = build_role_es_positive_filters(
        intent, role, exclude_slots=tuple(_pos_excl),
    )
    if role_pos_filters:
        es_query = _merge_es_filters(es_query, role_pos_filters)
    role_must_not = build_role_es_must_not(intent, role)
    if role_must_not:
        es_query = _merge_es_must_not(es_query, role_must_not)

    # per-role 版型（global←覆盖，同义词归并展开为 terms）。
    # skip_modeling=True（query2es 0 命中兜底 / progressive relax）时跳过，放宽版型约束以恢复召回。
    from backend.intent.sku_attributes import expand_modeling
    _eff = effective_role_slots(intent, role)
    _eff_modeling = _eff.get("modeling")
    if not skip_modeling and _eff_modeling and _eff_modeling not in ("n/a", ""):
        _m_exp = expand_modeling(str(_eff_modeling))
        if _m_exp:
            es_query = _merge_es_filters(es_query, [{"terms": {"modeling": _m_exp}}])
    # per-role 价格区间（global←覆盖）；skip_price（progressive relax 链尾放宽）时跳过。
    if not skip_price:
        _bmin, _bmax = effective_role_budget(intent, role)
        _rng: dict[str, Any] = {}
        if _bmin and _bmin > 0:
            _rng["gte"] = _bmin
        if _bmax and _bmax > 0:
            _rng["lte"] = _bmax
        if _rng:
            es_query = _merge_es_filters(es_query, [{"range": {"price": _rng}}])

    meta: dict[str, Any] = {
        "fallback_q": fallback_q,
        "source": source,
        "target_role": role,
        "index_name": index_name,
        "llm_enabled": llm_enabled,
        "category_l2_pairing_list": get_pairing_list_mode(),
    }
    role_companions = filter_companions_for_target_role(
        allowed_companion_cat2,
        role,
    )
    if role_companions:
        meta["category_l2_pairing_filter"] = list(role_companions)
    if allowed_companion_color_series:
        meta["color_series_pairing_filter"] = list(allowed_companion_color_series)
    if parsed:
        meta["llm_parsed_keys"] = list(parsed.keys())[:20]
    return es_query, meta
