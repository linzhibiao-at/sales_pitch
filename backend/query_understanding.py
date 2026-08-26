"""文本意图：轻量级 Trie 提取 + 规则兜底 + LLM fallback。"""

from __future__ import annotations

import re
from typing import Any, Optional

from backend.intent.intent_engine import IntentResult, extract_intent
from backend.intent.slot_defs import REQUIRED_ROLES, ROLE_EN_TO_ZH, ROLE_ZH_TO_EN
from backend.models import UserIntent, normalize_age, normalize_gender, normalize_gender_first, normalize_season

_INTENT_GENDER_ENUM = frozenset({"男", "女", "男童", "女童", "儿童"})


def _normalize_season_list(raw: object) -> list[str]:
    return normalize_season(raw)


def _normalize_intent_gender(value: object) -> Optional[str]:
    return normalize_gender(value)


def _refine_intent_with_rules(intent: UserIntent, original: str) -> UserIntent:
    """LLM 粗判后的规则补强：季节词、全身 target_roles。"""
    t = (original or "").strip()
    # 季节兜底
    seasons = _normalize_season_list(intent.season)
    if not seasons:
        from backend.intent.intent_engine import _infer_season_from_date
        seasons = _infer_season_from_date()
    if seasons != intent.season:
        intent = intent.model_copy(update={"season": seasons})
    # 全身搭配
    upd: dict[str, Any] = {}
    want_full = any(
        k in t
        for k in ("一套", "全身", "整套", "完整搭配", "从上到下", "一身穿搭", "一身", "全套", "一整套")
    )
    tr = list(intent.target_roles or [])
    if want_full and (not tr or set(tr) <= {"bottoms", "shoes"}):
        upd["target_roles"] = ["top", "bottoms", "shoes"]
    if upd:
        return intent.model_copy(update=upd)
    return intent


def parse_user_intent(
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
    """统一意图解析入口：使用轻量级 Trie + 规则 + LLM fallback。

    返回 IntentResult（包含 UserIntent + 调试信息）。
    """
    return extract_intent(
        text,
        image_base64=image_base64,
        image_anchor_row=image_anchor_row,
        image_similarity=image_similarity,
        image_candidate_rows=image_candidate_rows,
        model_override=model_override,
        session_context=session_context,
        sku_input_row=sku_input_row,
    )


def find_sku_token(text: str) -> Optional[str]:
    """在文本中查找货号/款号 token（支持连字符色码）。"""
    t = text or ""
    for pat in (
        r"\b([A-Z0-9]{2,}-[A-Z0-9]{2,})\b",
        r"\b([A-Z][A-Z0-9]{6,})\b",
    ):
        for m in re.finditer(pat, t):
            return m.group(1)
    return None


_CORRUPT_MARKERS = ('\\u', '\\"', '"table"', '"SIZE"', '[[', ']]')

_COMPLEMENTARY_ROLES_ZH = list(REQUIRED_ROLES)


def _is_valid_tag(s: str) -> bool:
    """过滤损坏/编码异常的商品属性值。"""
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


def backfill_intent_from_sku(
    intent: UserIntent,
    sku_row: dict[str, Any],
) -> UserIntent:
    """用 SKU 属性回填 UserIntent 中为空的字段（用户文本指定的优先）。"""
    if not sku_row:
        return intent
    upd: dict[str, Any] = {}
    if not intent.gender:
        g = normalize_gender_first(sku_row.get("gender"))
        if g:
            upd["gender"] = g
    if not intent.age:
        a = normalize_age(sku_row.get("age"))
        if a:
            upd["age"] = a
    if not intent.season:
        season = sku_row.get("season") or []
        if isinstance(season, str):
            season = [season]
        season = _normalize_season_list(season)
        if season:
            upd["season"] = season
    if not intent.color_series:
        cs = sku_row.get("color_series")
        if cs:
            cs_list = [cs] if isinstance(cs, str) else list(cs)
            cs_list = [str(x).strip() for x in cs_list if str(x).strip()]
            if cs_list:
                upd["color_series"] = cs_list
    if not intent.category:
        cat = str(sku_row.get("category_l2") or sku_row.get("category_l1") or "").strip()
        if cat and _is_valid_tag(cat):
            upd["category"] = [cat]
    if not intent.anchor_role:
        role = str(sku_row.get("role") or "").strip()
        if role:
            upd["anchor_role"] = role
            upd["query_type"] = "item_to_outfit"
            if not intent.target_roles:
                role_zh = ROLE_EN_TO_ZH.get(role, role)
                targets_zh = [r for r in _COMPLEMENTARY_ROLES_ZH if r != role_zh]
                upd["target_roles"] = [ROLE_ZH_TO_EN.get(r, r) for r in targets_zh]
    if not intent.style_tags:
        st = sku_row.get("style_tags") or []
        if isinstance(st, list) and st:
            upd["style_tags"] = [str(x).strip() for x in st
                                 if str(x).strip() and _is_valid_tag(str(x))]
    if upd:
        return intent.model_copy(update=upd)
    return intent
