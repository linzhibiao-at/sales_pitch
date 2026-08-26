"""意图引擎主入口：融合 图搜 + LLM 两路 slots。

Trie 不参与 slot 决策，仅保留在 `_normalize_categories` 中做品类名归一化。
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import date
from functools import lru_cache
from typing import Any, Optional

from backend.config import load_config
from backend.intent.confidence import compute_confidence
from backend.intent.slot_defs import REQUIRED_ROLES, ROLE_ZH_TO_EN
from backend.intent.trie_extractor import get_multi_slot_extractor
from backend.intent.color_series_mapper import map_color_to_series_list
from backend.intent.sku_attributes import (
    enrich_sku_attributes,
    extract_series_from_text,
    normalize_attr_enum,
    normalize_series,
)
from backend.models import UserIntent, normalize_age, normalize_gender, normalize_gender_first, normalize_season
from backend.ranking.scoring import gender_conflict, season_conflict

logger = logging.getLogger(__name__)

_IMAGE_PRIORITY_SLOTS = frozenset({"gender", "season", "anchor_role"})

_CORRUPT_MARKERS = ('\\u', '\\"', '"table"', '"SIZE"', '[[', ']]')

_EXPLICIT_GENDER_PATTERNS = re.compile(
    r"(男款|女款|男士|女士|男装|女装|男生|女生|男童|女童|儿童|童装|小男孩|小女孩|男宝|女宝)"
)
_EXPLICIT_AGE_PATTERNS = re.compile(
    r"(小童|中大童|大童|中童|婴幼童|婴儿|婴幼|幼童|通码)"
)
_EXPLICIT_SEASON_PATTERNS = re.compile(
    r"(春夏|秋冬|春天|夏天|秋天|冬天|春季|夏季|秋季|冬季|初春|盛夏|深秋|寒冬|酷暑|三伏)"
)

# 融合中需要跟踪来源的 slot 名列表
_TRACKED_SLOTS = (
    "gender", "age", "season", "anchor_role", "style_tags",
    "occasion_tags", "color", "color_series", "category",
)


def _is_clean_attr(s: str) -> bool:
    s = s.strip()
    if not s:
        return False
    if any(m in s for m in _CORRUPT_MARKERS):
        return False
    if s.startswith('[') or s.startswith('{'):
        return False
    if len(s) > 50:
        return False
    return True


def _is_explicit_mention(text: str, slot: str) -> bool:
    """判断用户文本是否明确指定了某 slot，明确指定时不可被图搜/LLM 覆盖。"""
    t = (text or "").strip()
    if slot == "gender":
        return bool(_EXPLICIT_GENDER_PATTERNS.search(t))
    if slot == "age":
        return bool(_EXPLICIT_AGE_PATTERNS.search(t))
    if slot == "season":
        return bool(_EXPLICIT_SEASON_PATTERNS.search(t))
    return False


_SKU_NOISE_WORDS = ("货号", "款号", "商品", "编号", "sku", "id", "code")


def _is_sku_only_text(text: str, sku_id: str) -> bool:
    """判断文本是否仅为 sku_id（无其他实质意图词），用于跳过 LLM。

    文本为空、或仅含 sku_id 及“货号/款号”等无意义前缀时返回 True。
    """
    t = (text or "").strip()
    if not t:
        return True
    if not sku_id:
        return False
    removed = t.replace(sku_id, " ")
    for w in _SKU_NOISE_WORDS:
        removed = removed.replace(w, " ")
    residual = re.sub(r"[\W_]+", "", removed, flags=re.UNICODE)
    return not residual


def _infer_season_from_date(today: date | None = None) -> list[str]:
    d = today or date.today()
    month = d.month
    if month in (3, 4, 5):
        return ["春"]
    if month in (6, 7, 8):
        return ["夏"]
    if month in (9, 10, 11):
        return ["秋"]
    return ["冬"]


# ── 各路 slot 提取 ────────────────────────────────────────────


def _extract_slots_from_sku_row(sku_row: dict[str, Any]) -> dict[str, list[str]]:
    slots: dict[str, list[str]] = {}

    gender = normalize_gender_first(sku_row.get("gender"))
    if gender:
        slots["gender"] = [gender]

    age = normalize_age(sku_row.get("age"))
    if age:
        slots["age"] = [age]

    season = normalize_season(sku_row.get("season"))
    if season:
        slots["season"] = season

    role = str(sku_row.get("role") or "").strip()
    if role:
        from backend.intent.slot_defs import ROLE_EN_TO_ZH
        role_zh = ROLE_EN_TO_ZH.get(role, role)
        slots["anchor_role"] = [role_zh]

    style = sku_row.get("style")
    if isinstance(style, list) and style:
        slots["style_tags"] = [str(s) for s in style if s and _is_clean_attr(str(s))]
    elif isinstance(style, str) and style.strip() and _is_clean_attr(style.strip()):
        slots["style_tags"] = [style.strip()]

    category = str(
        sku_row.get("category_l2")
        or sku_row.get("category_l1")
        or sku_row.get("category")
        or ""
    ).strip()
    if category and _is_clean_attr(category):
        slots["category"] = [category]

    color = str(
        sku_row.get("color") or sku_row.get("color_name")
        or sku_row.get("attr_name") or ""
    ).strip()
    if color:
        slots["color"] = [color]
        cs_list = map_color_to_series_list(color)
        if cs_list:
            slots["color_series"] = cs_list

    # 结构化属性（高 sim 匹配 SKU 时为权威 image 源）
    for attr_key in ("length_class", "coverage", "scene_domain"):
        v = str(sku_row.get(attr_key) or "").strip()
        if v:
            slots[attr_key] = [v]

    # series（子品牌线，SKU 数据权威；开放枚举，normalize_series 校验）
    series = str(sku_row.get("series") or "").strip()
    if series:
        ns = normalize_series(series)
        if ns:
            slots["series"] = [ns]

    return slots


def _build_sku_attr_block(sku_row: dict[str, Any]) -> str:
    """把 SKU 结构化属性格式化为可注入 LLM 的【锚点商品属性】文本块。

    用于 SKU 输入时让 LLM 在已知锚点属性约束下解析文本/图片意图，
    替代事后 backfill 作为主来源。空 row 或无可用属性时返回空串。
    """
    if not sku_row:
        return ""
    slots = _extract_slots_from_sku_row(sku_row)
    # _extract_slots_from_sku_row 已含 length_class/coverage/scene_domain，setdefault 兜底
    for k in ("length_class", "coverage", "scene_domain"):
        v = str(sku_row.get(k) or "").strip()
        if v:
            slots.setdefault(k, [v])
    if not slots:
        return ""
    lines = [f"{k}={'/'.join(v)}" for k, v in slots.items()]
    return (
        "【锚点商品属性】用户已锁定以下单品，以下属性为权威值，"
        "输出须与之一致，不得与之矛盾：\n" + "\n".join(lines)
    )


def _extract_slots_by_vote(
    candidate_rows: list[dict[str, Any]],
    min_similarity: float,
    *,
    filter_gender: str | None = None,
    filter_season: list[str] | None = None,
) -> dict[str, list[str]]:
    eligible = [
        r for r in candidate_rows
        if float(r.get("_image_similarity", 0)) >= min_similarity
    ]
    # 文本已知 gender/season 时预过滤
    if filter_gender:
        eligible = [r for r in eligible if not gender_conflict(r.get("gender"), filter_gender)]
    if filter_season:
        eligible = [r for r in eligible if not season_conflict(r.get("season"), filter_season)]
    if not eligible:
        return {}
    slot_counters: dict[str, Counter] = {
        "gender": Counter(),
        "age": Counter(),
        "season": Counter(),
        "anchor_role": Counter(),
        "style_tags": Counter(),
        "color": Counter(),
        "color_series": Counter(),
        "category": Counter(),
    }
    for row in eligible:
        row_slots = _extract_slots_from_sku_row(row)
        for slot, values in row_slots.items():
            if slot in slot_counters:
                for v in values:
                    slot_counters[slot][v] += 1
    result: dict[str, list[str]] = {}
    for slot, counter in slot_counters.items():
        if counter:
            max_count = counter.most_common(1)[0][1]
            winners = [v for v, c in counter.most_common() if c == max_count]
            result[slot] = winners
    return result


def _filter_candidates_by_intent(
    candidate_rows: list[dict[str, Any]],
    intent_gender: str | None,
    intent_season: list[str] | None,
    intent_age: str | None = None,
) -> list[dict[str, Any]]:
    """用最终确定的 gender/season/age 过滤候选 SKU，供后续固定搭配检索使用。"""
    if not candidate_rows:
        return []
    filtered = candidate_rows
    if intent_gender:
        filtered = [r for r in filtered if not gender_conflict(r.get("gender"), intent_gender)]
    if intent_age:
        from backend.ranking.scoring import age_conflict
        filtered = [r for r in filtered if not age_conflict(r.get("age"), intent_age)]
    if intent_season:
        filtered = [r for r in filtered if not season_conflict(r.get("season"), intent_season)]
    return filtered


def _intent_to_slots(intent: UserIntent) -> dict[str, list[str]]:
    """从 UserIntent 反向提取 slots dict，用于三方融合。"""
    slots: dict[str, list[str]] = {}
    if intent.gender:
        slots["gender"] = [intent.gender]
    if intent.age:
        slots["age"] = [intent.age]
    if intent.season:
        slots["season"] = list(intent.season)
    if intent.style_tags:
        slots["style_tags"] = list(intent.style_tags)
    if intent.occasion_tags:
        slots["occasion_tags"] = list(intent.occasion_tags)
    if intent.color:
        slots["color"] = list(intent.color)
    if intent.color_series:
        slots["color_series"] = list(intent.color_series)
    if intent.category:
        slots["category"] = list(intent.category)
    # 结构化属性（LLM 源；中性值 n/a/"" 不入 slots，让 enrich 兜底）
    for attr_key in ("length_class", "coverage", "scene_domain"):
        v = getattr(intent, attr_key, None)
        if v and v not in ("n/a", ""):
            slots[attr_key] = [v]
    if intent.series:
        slots["series"] = [intent.series]
    if intent.anchor_role:
        from backend.intent.slot_defs import ROLE_EN_TO_ZH
        role_zh = ROLE_EN_TO_ZH.get(intent.anchor_role, intent.anchor_role)
        slots["anchor_role"] = [role_zh]
    return slots


# ── 两源融合（图搜 + LLM） ────────────────────────────────────


def _merge_two_sources(
    image_slots: dict[str, list[str]],
    llm_slots: dict[str, list[str]],
    *,
    original_text: str = "",
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """两源 slots 融合，返回 (merged_slots, slot_sources)。

    slot_sources: {slot_name: "image"|"llm"|"none"}

    融合优先级：
    1. 优先字段（gender/season/anchor_role，图搜权威）且文本明确提及且 LLM 有值 → llm
    2. 优先字段且图搜有值 → image
    3. 优先字段且 LLM 有值 → llm
    4. 其余字段：LLM 有值 → llm（用户文本偏好优先于图搜属性）
    5. 其余字段：图搜有值 → image
    6. 无来源 → none

    说明：Trie 退出决策后，文本来源由 LLM 承担（LLM 同时读文本与图）。
    """
    merged: dict[str, list[str]] = {}
    sources: dict[str, str] = {}
    all_keys = set(image_slots) | set(llm_slots)

    for key in all_keys:
        iv = image_slots.get(key, [])
        lv = llm_slots.get(key, [])

        if key in _IMAGE_PRIORITY_SLOTS:
            text_explicit = _is_explicit_mention(original_text, key)
            if text_explicit and lv:
                merged[key] = lv
                sources[key] = "llm"
            elif iv:
                merged[key] = iv
                sources[key] = "image"
            elif lv:
                merged[key] = lv
                sources[key] = "llm"
            else:
                merged[key] = []
                sources[key] = "none"
        else:
            if lv:
                merged[key] = lv
                sources[key] = "llm"
            elif iv:
                merged[key] = iv
                sources[key] = "image"
            else:
                merged[key] = []
                sources[key] = "none"

    return merged, sources


# ── slots_detail 构建 ─────────────────────────────────────────


def _build_slots_detail(
    intent: UserIntent,
    slot_sources: dict[str, str],
    hits_detail: dict[str, list[dict[str, str]]],
) -> dict[str, Any]:
    """从最终 intent + slot_sources 构建调试面板的 slots_detail。

    每个 slot 的 source 直接来自融合时的确定结果，不再猜测。
    """
    slot_field_map: dict[str, list[str]] = {
        "season": list(intent.season or []),
        "style_tags": list(intent.style_tags or []),
        "gender": [intent.gender] if intent.gender else [],
        "age": [intent.age] if intent.age else [],
        "occasion_tags": list(intent.occasion_tags or []),
        "color": list(intent.color or []),
        "color_series": list(intent.color_series or []),
        "category": list(intent.category or []),
        "length_class": [intent.length_class] if intent.length_class else [],
        "coverage": [intent.coverage] if intent.coverage else [],
        "scene_domain": [intent.scene_domain] if intent.scene_domain else [],
        "series": [intent.series] if intent.series else [],
    }
    detail: dict[str, Any] = {}
    for slot_name, values in slot_field_map.items():
        hits = hits_detail.get(slot_name, [])
        detail[slot_name] = {
            "values": values,
            "source": slot_sources.get(slot_name, "none"),
            "dict_hits": [h.get("keyword", "") for h in hits] if hits else [],
        }
    detail["anchor_role"] = {
        "values": [intent.anchor_role] if intent.anchor_role else [],
        "source": slot_sources.get("anchor_role", "none"),
        "dict_hits": [],
    }
    detail["target_roles"] = {
        "values": list(intent.target_roles or []),
        "source": slot_sources.get("target_roles", "none"),
        "dict_hits": [],
    }
    detail["target_slots"] = {
        "values": dict(intent.target_slots or {}),
        "source": slot_sources.get("target_slots", "llm"),
        "dict_hits": [],
    }
    return detail


# ── 辅助函数 ──────────────────────────────────────────────────


def _roles_zh_to_en(roles: list[str]) -> list[str]:
    return [ROLE_ZH_TO_EN.get(r, r) for r in roles]


def _backfill_color_series(intent: UserIntent) -> UserIntent:
    if intent.color_series:
        return intent
    colors = intent.color or []
    if not colors:
        return intent
    series: list[str] = []
    seen: set[str] = set()
    for c in colors:
        for s in map_color_to_series_list(str(c).strip()):
            if s and s not in seen:
                series.append(s)
                seen.add(s)
    if series:
        intent.color_series = series
    return intent


def _backfill_series(intent: UserIntent, text: str) -> UserIntent:
    """LLM 漏抽 series 时的确定性安全网：从原文扫描 canonical series。

    仅当文字含显式系列信号（「X系列」「X联名」「X的」或「X+中文品类词」）才回填，
    避免误报致 0 召回。详见 sku_attributes.extract_series_from_text。
    """
    if intent.series:
        return intent
    hit = extract_series_from_text(text or "")
    if hit:
        intent.series = hit
    return intent


def _slots_to_intent(
    text: str,
    slots: dict[str, list[str]],
    anchor_role: str | None,
    target_roles: list[str],
    *,
    target_slots: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> UserIntent:
    season = normalize_season(slots.get("season", []))
    if not season:
        season = _infer_season_from_date()

    anchor_en = ROLE_ZH_TO_EN.get(anchor_role, anchor_role) if anchor_role else None
    targets_en = _roles_zh_to_en(target_roles)

    query_type = "item_to_outfit" if anchor_en else "text_only"

    gender_list = slots.get("gender", [])
    gender = normalize_gender(gender_list[0]) if gender_list else None

    age_list = slots.get("age", [])
    age = normalize_age(age_list[0]) if age_list else None

    return UserIntent(
        query_type=query_type,
        text=text,
        anchor_role=anchor_en,
        target_roles=targets_en,
        gender=gender,
        age=age,
        season=season,
        occasion_tags=slots.get("occasion_tags", []),
        style_tags=slots.get("style_tags", []),
        color=slots.get("color", []),
        color_series=slots.get("color_series", []),
        category=slots.get("category", []),
        length_class=normalize_attr_enum("length_class", (slots.get("length_class") or [""])[0]) or None,
        coverage=normalize_attr_enum("coverage", (slots.get("coverage") or [""])[0]) or None,
        scene_domain=normalize_attr_enum("scene_domain", (slots.get("scene_domain") or [""])[0]),
        series=normalize_series((slots.get("series") or [""])[0]) or None,
        target_slots=target_slots or {},
    )


def _backfill_from_context(intent: UserIntent, context: dict[str, Any] | None) -> UserIntent:
    """从多轮对话上下文回填空缺字段。"""
    if not context:
        return intent
    prev = context.get("prev_intent")
    if not prev or not isinstance(prev, dict):
        return intent
    upd: dict[str, Any] = {}
    if not intent.gender and prev.get("gender"):
        upd["gender"] = prev["gender"]
    if not intent.age and prev.get("age"):
        upd["age"] = prev["age"]
    if not intent.season and prev.get("season"):
        upd["season"] = prev["season"]
    if not intent.style_tags and prev.get("style_tags"):
        upd["style_tags"] = prev["style_tags"]
    if not intent.occasion_tags and prev.get("occasion_tags"):
        upd["occasion_tags"] = prev["occasion_tags"]
    if not intent.color and prev.get("color"):
        upd["color"] = prev["color"]
    if not intent.color_series and prev.get("color_series"):
        upd["color_series"] = prev["color_series"]
    if not intent.category and prev.get("category"):
        upd["category"] = prev["category"]
    if not intent.series and prev.get("series"):
        upd["series"] = prev["series"]
    if not intent.target_slots and prev.get("target_slots"):
        upd["target_slots"] = prev["target_slots"]
    if upd:
        return intent.model_copy(update=upd)
    return intent


def _resolve_anchor_and_targets(
    text: str,
    merged_slots: dict[str, list[str]],
    llm_intent: UserIntent | None,
    image_anchor_row: dict[str, Any] | None,
    image_similarity: float,
    sim_threshold: float,
) -> tuple[str | None, list[str]]:
    """角色解析：意图识别只认 LLM，正则/Trie 不参与。

    LLM 权威：有 ``llm_intent`` 时，``anchor_role`` / ``target_roles`` 一律以 LLM
    返回为准（prompt 已硬约束 target_roles 不含 anchor_role 且遵循互补映射）。
    仅当 LLM 缺失（调用失败）或未给角色时，才用图搜 anchor 兜底 + 互补映射补全
    target_roles——这是 LLM 不可用时的降级，不是正则意图识别。
    """
    from backend.intent.slot_defs import ROLE_EN_TO_ZH

    anchor_role: str | None = None
    target_roles: list[str] = []

    # LLM 权威值优先
    if llm_intent:
        if llm_intent.anchor_role:
            anchor_role = ROLE_EN_TO_ZH.get(llm_intent.anchor_role, llm_intent.anchor_role)
        if llm_intent.target_roles:
            target_roles = [
                ROLE_EN_TO_ZH.get(r, r) for r in llm_intent.target_roles if str(r).strip()
            ]

    # 图搜 anchor_role：仅在 LLM 缺失时兜底（异款风格参考图已在 step2 剥离优先字段）
    if not anchor_role and image_anchor_row and image_similarity >= sim_threshold:
        ar_list = merged_slots.get("anchor_role") or []
        if ar_list:
            anchor_role = ar_list[0]

    # 有 anchor 无 target：按互补映射兜底（LLM 失败/漏抽时）
    if anchor_role and not target_roles:
        target_roles = [r for r in REQUIRED_ROLES if r != anchor_role]

    # 去重 + 剔除 anchor_role（target 不得含 anchor）
    target_roles = [r for r in dict.fromkeys(target_roles) if r != anchor_role]

    return anchor_role, target_roles


# ── IntentResult ──────────────────────────────────────────────


def resolve_anchor_attrs(
    intent: UserIntent,
    image_anchor_row: dict[str, Any] | None,
    image_similarity: float,
    sim_threshold: float,
) -> dict[str, Any]:
    """融合产出锚点结构化属性，供召回各通路统一消费。

    返回 ``{role, category_l2, length_class, coverage, layer, scene_domain,
    is_intimate, gender, season}``。召回层不再各自派生/特判虚拟图锚点——
    虚拟锚点也带 ``category_l2``，``length_class`` 不再退化为 ``"n/a"``。

    优先级：
      - role：``intent.anchor_role``（三源融合后的角色判定）优先，回退匹配 SKU。
      - 高 sim(>=threshold) 且有匹配 SKU：继承 SKU 已持久化的
        ``category_l2/length_class/coverage/layer/scene_domain/is_intimate``
        （保留 VLM 回补精度）。
      - 否则（虚拟图锚点 / 低 sim）：``category_l2`` 取 ``intent.category[0]``
        （图搜+LLM 融合结果），其余属性由 ``enrich_sku_attributes``
        从 (role+category_l2) 派生（仅填缺失键，不覆盖已有）。
    """
    role = (intent.anchor_role or "").strip()
    cat2 = ""
    persisted: dict[str, Any] = {}

    if image_anchor_row and image_similarity >= sim_threshold:
        # 权威真实 SKU 锚点：继承其持久化属性（含 VLM 回补的 length_class 等）
        for k in ("category_l2", "length_class", "coverage",
                  "layer", "scene_domain", "is_intimate", "series"):
            v = image_anchor_row.get(k)
            if v not in (None, "", []):
                persisted[k] = v
        cat2 = str(image_anchor_row.get("category_l2") or "").strip()
        if not role:
            role = str(image_anchor_row.get("role") or "").strip()

    # 虚拟/低 sim：用融合后的 intent.category 作为 category_l2
    if not cat2 and intent.category:
        cat2 = str(intent.category[0] or "").strip()

    base: dict[str, Any] = {
        "category_l2": cat2,
        "gender": intent.gender,
        "age": intent.age,
        "season": list(intent.season or []),
    }
    base.update(persisted)  # 已持久化属性优先
    # 意图模块（LLM 看图）解析的结构化属性填补 persisted 未覆盖或非确定的键；
    # n/a/"" 视为「无信号」，交给 enrich 从 category 派生（避免屏蔽冲锋衣→long 等）。
    for k in ("length_class", "coverage", "scene_domain"):
        iv = getattr(intent, k, None)
        if iv and iv not in ("n/a", "") and base.get(k) in (None, "", "n/a"):
            base[k] = iv
    # series：SKU 持久化优先（权威），否则回退 intent.series（text_only 用户显式提系列）
    if not base.get("series") and intent.series:
        base["series"] = intent.series
    base["role"] = role  # 融合角色判定优先于 SKU 原始 role
    enrich_sku_attributes(base)  # 仅填缺失键
    return base


class IntentResult:
    def __init__(
        self,
        intent: UserIntent,
        method: str,
        confidence: float,
        slots_detail: dict[str, Any],
        llm_fallback: bool = False,
        image_override: bool = False,
        image_override_slots: list[str] | None = None,
        per_slot_confidence: dict[str, float] | None = None,
        filtered_candidate_rows: list[dict[str, Any]] | None = None,
        source_slots: dict[str, dict[str, list[str]]] | None = None,
        anchor_attrs: dict[str, Any] | None = None,
        anchor_source: str = "none",
        image_role: str = "none",
    ) -> None:
        self.intent = intent
        self.method = method
        self.confidence = confidence
        self.slots_detail = slots_detail
        self.llm_fallback = llm_fallback
        self.image_override = image_override
        self.image_override_slots = image_override_slots or []
        self.per_slot_confidence = per_slot_confidence or {}
        self.filtered_candidate_rows = filtered_candidate_rows
        self.source_slots = source_slots or {}
        self.anchor_attrs = anchor_attrs  # 融合后的锚点结构化属性（召回统一消费）
        self.anchor_source = anchor_source  # 锚点来源: sku/image/none
        self.image_role = image_role  # 用户图角色: anchor/style_ref/none

    def to_sse_fields(self) -> dict[str, Any]:
        fields = {
            "method": self.method,
            "confidence": round(self.confidence, 4),
            "llm_fallback": self.llm_fallback,
            "slots_detail": self.slots_detail,
            "image_override": self.image_override,
            "image_override_slots": self.image_override_slots,
            "anchor_source": self.anchor_source,
            "image_role": self.image_role,
        }
        if self.per_slot_confidence:
            fields["per_slot_confidence"] = {
                k: round(v, 4) for k, v in self.per_slot_confidence.items()
            }
        if self.source_slots:
            fields["source_slots"] = self.source_slots
        if self.anchor_attrs:
            fields["anchor_attrs"] = self.anchor_attrs
        return fields


# ── 纯 SKU 输入：直接用 SKU 属性填充/推理（不调用 LLM） ───────


def _build_intent_from_sku_only(
    text: str,
    sku_row: dict[str, Any],
    session_context: dict[str, Any] | None,
    *,
    image_candidate_rows: list[dict[str, Any]] | None = None,
) -> IntentResult:
    """纯 SKU 输入：直接用 SKU 属性填充/推理意图，不调用 LLM。"""
    sku_slots = _extract_slots_from_sku_row(sku_row)

    anchor_role_zh = (sku_slots.get("anchor_role") or [None])[0]
    target_roles: list[str] = []
    if anchor_role_zh:
        target_roles = [r for r in REQUIRED_ROLES if r != anchor_role_zh]

    intent = _slots_to_intent(text, sku_slots, anchor_role_zh, target_roles)
    intent = _backfill_color_series(intent)
    intent = _backfill_series(intent, text)
    intent = _backfill_from_context(intent, session_context)

    confidence, per_slot_conf = compute_confidence(
        {},
        has_image=False,
        image_slots=sku_slots,
        anchor_role=anchor_role_zh,
        target_roles=target_roles,
    )

    slot_sources: dict[str, str] = {k: "sku" for k, v in sku_slots.items() if v}
    slot_sources["target_roles"] = "sku" if target_roles else "none"
    slots_detail = _build_slots_detail(intent, slot_sources, {})

    filtered_candidate_rows = _filter_candidates_by_intent(
        image_candidate_rows or [],
        intent.gender,
        list(intent.season or []) or None,
        intent_age=intent.age,
    ) if image_candidate_rows else None

    logger.info(
        "[SKU纯输入] sku_id=%s, 跳过 LLM, role=%s, gender=%s, age=%s, season=%s",
        sku_row.get("sku_id"), intent.anchor_role, intent.gender, intent.age, intent.season,
    )

    return IntentResult(
        intent=intent,
        method="sku_only",
        confidence=confidence,
        slots_detail=slots_detail,
        llm_fallback=False,
        image_override=False,
        image_override_slots=[],
        per_slot_confidence=per_slot_conf,
        filtered_candidate_rows=filtered_candidate_rows,
        source_slots={
            "image": {},
            "llm": {},
            "sku": {k: v for k, v in sku_slots.items() if v},
        },
        anchor_attrs=resolve_anchor_attrs(intent, sku_row, 1.0, 0.9),
    )


# ── 主入口 ────────────────────────────────────────────────────


def extract_intent(
    text: str,
    *,
    image_base64: str | None = None,
    image_anchor_row: dict[str, Any] | None = None,
    image_similarity: float = 0.0,
    image_candidate_rows: list[dict[str, Any]] | None = None,
    model_override: str | None = None,
    session_context: dict[str, Any] | None = None,
    sku_input_row: dict[str, Any] | None = None,
) -> IntentResult:
    """意图提取主入口：提取 图搜 + LLM 两路 slots，然后融合（Trie 不参与决策）。

    流程：
      0. 锚点选举：判定 anchor_source(sku/image/none) 与 image_role(anchor/style_ref/none)
      1. LLM 调用决策：仅当唯一信号是结构化 SKU（无文本、无图）时短路跳过 LLM；
         否则调用 LLM，并把 SKU 属性注入 LLM 上下文（权威值）
      2. 图搜近邻属性提取 → image_slots（投票/top1，带 gender/season 预过滤）
         image_role=style_ref 时剥离 gender/season/anchor_role，避免异款图覆盖 SKU 权威值
      3. 两源融合 → merged_slots + slot_sources
      4. 角色解析 → anchor_role, target_roles
      5. 构建 UserIntent + slots_detail（每个 slot 标注来源）
      6. 最终 gender/season 二次过滤候选 SKU

    Args:
        text: 用户输入文本
        image_base64: 用户上传图片（base64）
        image_anchor_row: 图搜 top1 的 SKU 行数据
        image_similarity: 图搜 top1 的相似度
        image_candidate_rows: 图搜多 SKU 候选行
        model_override: LLM 模型覆盖
        session_context: 多轮对话上下文（含 prev_intent 等）
        sku_input_row: 用户直接输入的 SKU 行数据（注入 LLM；纯 SKU 输入时跳过 LLM）

    Returns:
        IntentResult 包含 UserIntent 和调试信息
    """
    cfg = load_config()
    intent_cfg = cfg.get("intent") or {}
    sim_threshold = float(intent_cfg.get("image_sim_override_threshold") or 0.9)

    has_text = bool((text or "").strip())
    has_image = bool(image_base64)

    # ── Step 0: 锚点选举 ────────────────────────────────────
    sku_id_str = str(sku_input_row.get("sku_id") or "") if sku_input_row else ""
    img_anchor_id = str(image_anchor_row.get("sku_id") or "") if image_anchor_row else ""
    # 用户图与 SKU 同款 → 图作确认(anchor)；异款 → 图作风格参考(style_ref)
    same_sku = bool(sku_id_str and img_anchor_id and sku_id_str == img_anchor_id)
    if has_image and sku_input_row and not same_sku:
        image_role = "style_ref"
    elif has_image:
        image_role = "anchor"
    else:
        image_role = "none"
    anchor_source = "sku" if sku_input_row else ("image" if image_anchor_row else "none")

    # ── Step 1: LLM 调用决策 + SKU 属性注入 ─────────────────
    # 统一规则：仅当唯一信号是结构化 SKU（无图、文本为空或仅为 sku_id）时跳过 LLM
    if sku_input_row and not has_image and _is_sku_only_text(text, sku_id_str):
        result = _build_intent_from_sku_only(
            text or "", sku_input_row, session_context,
            image_candidate_rows=image_candidate_rows,
        )
        result.anchor_source = anchor_source
        result.image_role = image_role
        return result

    llm_slots: dict[str, list[str]] = {}
    llm_intent: UserIntent | None = None
    llm_ok = False

    if has_text or has_image:
        try:
            from backend.llm_client import extract_intent_json
            anchor_attr_text = _build_sku_attr_block(sku_input_row) if sku_input_row else None
            raw = extract_intent_json(
                text or "",
                image_base64=image_base64,
                model_override=model_override,
                anchor_attr_text=anchor_attr_text,
                image_role=image_role,
            )
            if raw:
                llm_intent = _build_intent_from_llm(text or "", raw)
                llm_slots = _intent_to_slots(llm_intent)
                llm_ok = True
        except Exception:
            logger.exception("LLM 意图提取失败，将仅使用 image 结果")

    # ── Step 2: 图搜 slots 提取 ──────────────────────────────
    image_slots: dict[str, list[str]] = {}

    slot_mode = str(intent_cfg.get("image_slot_mode") or "vote")

    if slot_mode == "vote" and image_candidate_rows:
        vote_min_sim = float(intent_cfg.get("image_vote_min_similarity") or 0.7)
        # 用 LLM 已知的 gender/season 预过滤候选再投票（Trie 退出后文本来源改由 LLM 承担）
        text_gender = llm_slots.get("gender", [None])[0] if llm_slots.get("gender") else None
        text_season = llm_slots.get("season", [])
        image_slots = _extract_slots_by_vote(
            image_candidate_rows, vote_min_sim,
            filter_gender=text_gender,
            filter_season=text_season or None,
        )
    elif image_anchor_row and image_similarity >= sim_threshold:
        image_slots = _extract_slots_from_sku_row(image_anchor_row)

    # 异款风格参考图不得贡献优先字段，避免覆盖 SKU 权威 gender/season/anchor_role
    if image_role == "style_ref":
        for k in ("gender", "season", "anchor_role"):
            image_slots.pop(k, None)

    # ── Step 3: 图搜 + LLM 两源融合 ─────────────────────────
    merged_slots, slot_sources = _merge_two_sources(
        image_slots, llm_slots,
        original_text=text or "",
    )

    # ── Step 5: 角色解析 ─────────────────────────────────────
    anchor_role, target_roles = _resolve_anchor_and_targets(
        text or "", merged_slots, llm_intent,
        image_anchor_row, image_similarity, sim_threshold,
    )

    # ── Step 6: 构建 UserIntent ──────────────────────────────
    # per-role 数据（target_slots 含 positive/negative）属 LLM 源、不参与 flat 两源融合，
    # 直接由 llm_intent 回贴到重建后的 intent（_slots_to_intent 会重建丢掉它们）。
    llm_target_slots = getattr(llm_intent, "target_slots", {}) if llm_intent else {}
    intent = _slots_to_intent(
        text or "", merged_slots, anchor_role, target_roles,
        target_slots=llm_target_slots,
    )
    intent = _backfill_color_series(intent)
    intent = _backfill_series(intent, text)
    intent = _backfill_from_context(intent, session_context)

    # 置信度（Trie 退出后，文本来源由 LLM 承担，第一参数传 llm_slots）
    confidence, per_slot_conf = compute_confidence(
        llm_slots,
        has_image=has_image,
        image_slots=image_slots,
        anchor_role=anchor_role,
        target_roles=target_roles,
    )

    # slots_detail: 每个 slot 的值和来源
    slots_detail = _build_slots_detail(intent, slot_sources, {})

    # method 标记：反映实际使用了哪些路径
    if llm_ok and image_slots:
        method = "image+llm"
    elif llm_ok:
        method = "llm"
    elif image_slots:
        method = "image"
    else:
        method = "none"

    # image_override: 哪些 slot 被图搜覆盖了文本值
    image_override_slots = [
        k for k, src in slot_sources.items()
        if src == "image" and k in _IMAGE_PRIORITY_SLOTS
    ]

    # ── Step 7: 二次过滤候选 SKU ─────────────────────────────
    final_gender = intent.gender
    final_season = list(intent.season or [])
    final_age = intent.age
    filtered_candidate_rows = _filter_candidates_by_intent(
        image_candidate_rows or [], final_gender, final_season or None, final_age,
    ) if image_candidate_rows else None

    return IntentResult(
        intent=intent,
        method=method,
        confidence=confidence,
        slots_detail=slots_detail,
        llm_fallback=False,
        image_override=bool(image_override_slots),
        image_override_slots=image_override_slots,
        per_slot_confidence=per_slot_conf,
        filtered_candidate_rows=filtered_candidate_rows,
        source_slots={
            "image": {k: v for k, v in image_slots.items() if v},
            "llm": {k: v for k, v in llm_slots.items() if v},
            "sku": _extract_slots_from_sku_row(sku_input_row) if sku_input_row else {},
        },
        anchor_attrs=resolve_anchor_attrs(
            intent, image_anchor_row, image_similarity, sim_threshold,
        ),
        anchor_source=anchor_source,
        image_role=image_role,
    )


# ── LLM 解析辅助 ─────────────────────────────────────────────


def _normalize_categories(categories: list[str]) -> list[str]:
    """将 LLM 返回的品类名通过 categories.yaml 字典归一化，
    再过滤为 category_l2_cartesian_pairing.yaml 中存在的合法中类。"""
    if not categories:
        return categories
    extractor = get_multi_slot_extractor()
    cat_extractor = extractor._extractors.get("category")
    if not cat_extractor or not cat_extractor._dict:
        return categories
    cat_dict = cat_extractor._dict
    result: list[str] = []
    seen: set[str] = set()
    for cat in categories:
        normalized = cat_dict.get(cat) or cat_dict.get(cat.lower()) or cat
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    # 过滤：只保留 category_l2_cartesian_pairing.yaml 中存在的中类
    valid = _valid_category_l2_set()
    if valid:
        filtered = [c for c in result if c in valid]
        if filtered != result:
            dropped = [c for c in result if c not in valid]
            if dropped:
                logger.debug("category_l2 dropped invalid LLM categories: %s", dropped)
        if not filtered:
            logger.info(
                "category_l2 valid-set 过滤后为空，保留归一化结果: %s", result,
            )
            return result
        return filtered
    return result


@lru_cache(maxsize=1)
def _valid_category_l2_set() -> frozenset[str]:
    """从 pairing YAML 加载合法中类集合。"""
    from backend.intent.category_l2_pairing import _load_pairing_data
    data = _load_pairing_data()
    rules = (data or {}).get("pairing_rules") or {}
    return frozenset(rules.keys())


def _coerce_str_list(val: Any) -> list[str]:
    """把 LLM 输入归一为非空字符串列表（None/标量/list 均兼容）。"""
    if val is None:
        return []
    if isinstance(val, list):
        return [str(v) for v in val if v is not None]
    if isinstance(val, str):
        return [val] if val.strip() else []
    return []


def _coerce_float(val: Any) -> Optional[float]:
    """把 LLM 输出的价格数值归一为正 float；None/非法/非正 → None。"""
    if val is None:
        return None
    try:
        f = float(val if not isinstance(val, list) else (val[0] if val else 0))
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


# 可在 target_slots/negative_slots 中出现的槽位名白名单
_PER_ROLE_LIST_SLOTS = ("color", "color_series", "category", "style_tags", "occasion_tags")
_PER_ROLE_SCALAR_SLOTS = ("length_class", "coverage", "scene_domain", "modeling")
# per-role 数值槽位（价格区间），非枚举，单独走 float 校验
_PER_ROLE_NUMERIC_SLOTS = ("budget_min", "budget_max")


def _normalize_role_key(role: Any) -> str:
    """role token 归一为英文（top/bottoms/shoes/dress/accessory）；保留 "*"。"""
    if role is None:
        return ""
    key = str(role).strip()
    if key == "*":
        return key
    key = key.lower()
    return ROLE_ZH_TO_EN.get(key, key)


def _normalize_positive(slot_map: Any, cleaned: dict[str, Any]) -> None:
    """归一 target_slots[role].positive：逐元素枚举校验，就地写入 cleaned。

    标量 slot 取首个合法值存标量；列表 slot 存 list；非法值丢弃。
    """
    if not isinstance(slot_map, dict):
        return
    for slot, val in slot_map.items():
        if val is None:
            continue
        slot = str(slot).strip()
        if slot in _PER_ROLE_SCALAR_SLOTS:
            for x in _coerce_str_list(val):
                v = normalize_attr_enum(slot, x)
                if v and v not in ("n/a", ""):
                    cleaned[slot] = v
                    break
        elif slot == "color_series":
            vals = [normalize_attr_enum("color_series", x) for x in _coerce_str_list(val)]
            vals = [v for v in vals if v]
            if vals:
                cleaned[slot] = vals
        elif slot == "category":
            vals = _normalize_categories(_coerce_str_list(val))
            if vals:
                cleaned[slot] = vals
        elif slot == "series":
            # series 是开放枚举（SERIES_LIST，子品牌线/联名胶囊），走 normalize_series
            # 而非 normalize_attr_enum（后者只认固定枚举）；标量取首个合法 canonical 值
            for x in _coerce_str_list(val):
                v = normalize_series(x)
                if v and v not in ("n/a", ""):
                    cleaned[slot] = v
                    break
        elif slot in _PER_ROLE_LIST_SLOTS:
            vals = [str(x).strip() for x in _coerce_str_list(val) if str(x).strip()]
            if vals:
                cleaned[slot] = vals
        elif slot in _PER_ROLE_NUMERIC_SLOTS:
            # 价格区间数值槽位：float 强转，正数才保留
            try:
                fval = float(val if not isinstance(val, list) else (val[0] if val else 0))
            except (TypeError, ValueError):
                continue
            if fval > 0:
                cleaned[slot] = fval


def _normalize_negative(slot_map: Any, cleaned: dict[str, list[str]]) -> None:
    """归一 target_slots[role].negative：逐元素枚举校验，就地写入 cleaned。

    所有 slot 值存 list；category 走 _normalize_neg_category（长度词转 length_class）；
    标量 slot 逐元素 normalize_attr_enum；非法值丢弃。
    """
    if not isinstance(slot_map, dict):
        return
    for slot, val in slot_map.items():
        slot = str(slot).strip()
        if slot in _PER_ROLE_SCALAR_SLOTS:
            for x in _coerce_str_list(val):
                v = normalize_attr_enum(slot, x)
                if v and v not in ("n/a", "") and v not in cleaned.get(slot, []):
                    cleaned.setdefault(slot, []).append(v)
        elif slot == "color_series":
            for x in _coerce_str_list(val):
                v = normalize_attr_enum("color_series", x)
                if v and v not in cleaned.get(slot, []):
                    cleaned.setdefault(slot, []).append(v)
        elif slot == "category":
            _normalize_neg_category(_coerce_str_list(val), cleaned)
        elif slot == "series":
            # series 开放枚举，逐元素 normalize_series；非法/非 canonical 值丢弃
            for x in _coerce_str_list(val):
                v = normalize_series(x)
                if v and v not in ("n/a", "") and v not in cleaned.get(slot, []):
                    cleaned.setdefault(slot, []).append(v)
        elif slot in _PER_ROLE_LIST_SLOTS:
            for x in _coerce_str_list(val):
                s = str(x).strip()
                if s and s not in cleaned.get(slot, []):
                    cleaned.setdefault(slot, []).append(s)


def _normalize_target_slots(raw: Any) -> dict[str, dict[str, dict[str, Any]]]:
    """归一 LLM 输出的 target_slots：{role: {positive, negative}}。

    role key→英文 token 或 "*"；"*" 仅承载 negative（positive 忽略）。
    返回 {role: {"positive": {...}, "negative": {...}}}，空 role 不产出。
    """
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for role, pn in raw.items():
        role_en = _normalize_role_key(role)
        if not role_en or not isinstance(pn, dict):
            continue
        cleaned_pos: dict[str, Any] = {}
        cleaned_neg: dict[str, list[str]] = {}
        if role_en != "*":
            _normalize_positive(pn.get("positive"), cleaned_pos)
        _normalize_negative(pn.get("negative"), cleaned_neg)
        if cleaned_pos or cleaned_neg:
            result[role_en] = {"positive": cleaned_pos, "negative": cleaned_neg}
    return result


# 长度语义聚合品类词 → length_class 枚举。用于把 LLM 对「不要短裤/长裤」输出的
# 非合法 L2 category 否定（如 短裤/短裤类/长裤）转为可匹配真实 SKU 的 length_class 否定。
_NEG_LENGTH_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("短裤", "short"), ("短裙", "short"), ("五分", "short"), ("七分", "short"),
    ("长裤", "long"), ("长裙", "long"), ("九分", "long"), ("打底裤", "long"),
)


def _neg_category_to_length(value: str) -> str | None:
    """长度语义聚合品类词 → length_class 枚举值；非长度词返回 None。"""
    s = value or ""
    for kw, lc in _NEG_LENGTH_KEYWORDS:
        if kw in s:
            return lc
    return None


def _normalize_neg_category(values: list[str], cleaned: dict[str, list[str]]) -> None:
    """归一 negative_slots 的 category 值，就地写入 cleaned。

    - 合法 L2（如 拖鞋/梭织短裤）→ 保留为 category 否定；
    - 非合法 L2 但为长度语义聚合词（短裤/短裤类/长裤/…）→ 转 length_class 否定
      （short/long，可匹配真实 SKU，避免 category_l2 != 短裤类 这类无效过滤）；
    - 字典归一后命中合法集的别名 → 保留为 category 否定；
    - 其余无法识别的值 → 丢弃并 debug 记录。
    """
    valid = _valid_category_l2_set()
    extractor = get_multi_slot_extractor()
    cat_ext = extractor._extractors.get("category") if extractor else None
    cat_dict = cat_ext._dict if cat_ext and cat_ext._dict else {}
    for raw_c in values:
        raw_s = str(raw_c).strip()
        if not raw_s:
            continue
        # 1. 具体 L2（raw 命中合法集）→ 保留为 category 否定
        if valid and raw_s in valid:
            if raw_s not in cleaned.get("category", []):
                cleaned.setdefault("category", []).append(raw_s)
            continue
        # 2. 非合法 L2 但为长度语义聚合词（短裤/短裤类/长裤/…）→ 转 length_class 否定
        #    必须先于字典归一：否则 "短裤" 会被字典映射成窄化的 "梭织短裤"，丢失「排除所有短裤」的语义
        lc = _neg_category_to_length(raw_s)
        if lc:
            v = normalize_attr_enum("length_class", lc)
            if v and v not in ("n/a", "") and v not in cleaned.get("length_class", []):
                cleaned.setdefault("length_class", []).append(v)
            continue
        # 3. 别名：字典归一后命中合法集 → 保留为 category 否定
        mapped = cat_dict.get(raw_s) or cat_dict.get(raw_s.lower()) or raw_s
        if valid and mapped in valid and mapped != raw_s:
            if mapped not in cleaned.get("category", []):
                cleaned.setdefault("category", []).append(mapped)
            continue
        # 4. 无法识别 → 丢弃
        logger.debug("negative_slots: drop non-enum category %r (no L2/length mapping)", raw_s)


def _strip_neg_pos_conflict(
    target_slots: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, dict[str, dict[str, Any]]]:
    """同 role 同 slot 不得既 positive 又 negative：剔该 role 的冲突否定项。

    "*" 无 positive，跳过。就地修改并返回。
    """
    for role, pn in target_slots.items():
        if role == "*":
            continue
        pos = pn.get("positive") or {}
        neg = pn.get("negative")
        if not neg:
            continue
        for slot in list(neg.keys()):
            if slot in pos:
                neg.pop(slot, None)
    return target_slots


def _build_intent_from_llm(text: str, raw: dict[str, Any]) -> UserIntent:
    from backend.query_understanding import (
        _normalize_intent_gender,
        _normalize_season_list,
        _refine_intent_with_rules,
    )

    def _to_str_list(val: Any) -> list[str]:
        return _coerce_str_list(val)

    target_slots = _normalize_target_slots(raw.get("target_slots"))
    target_slots = _strip_neg_pos_conflict(target_slots)

    try:
        parsed = UserIntent(
            query_type=raw.get("query_type") or "text_only",
            text=raw.get("text") or text,
            anchor_role=raw.get("anchor_role"),
            target_roles=_to_str_list(raw.get("target_roles")),
            gender=_normalize_intent_gender(raw.get("gender")),
            age=normalize_age(raw.get("age")),
            season=_normalize_season_list(raw.get("season")),
            occasion_tags=_to_str_list(raw.get("occasion_tags")),
            style_tags=_to_str_list(raw.get("style_tags")),
            color=_to_str_list(raw.get("color")),
            color_series=_to_str_list(raw.get("color_series")),
            category=_normalize_categories(_to_str_list(raw.get("category"))),
            length_class=normalize_attr_enum("length_class", raw.get("length_class")) or None,
            coverage=normalize_attr_enum("coverage", raw.get("coverage")) or None,
            scene_domain=normalize_attr_enum("scene_domain", raw.get("scene_domain")),
            series=normalize_series(raw.get("series")) or None,
            modeling=normalize_attr_enum("modeling", raw.get("modeling")) or None,
            budget_max=_coerce_float(raw.get("budget_max")),
            budget_min=_coerce_float(raw.get("budget_min")),
            target_slots=target_slots,
        )
        return _refine_intent_with_rules(parsed, text)
    except (TypeError, ValueError):
        return UserIntent(query_type="text_only", text=text)

