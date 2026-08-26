"""从意图识别结果生成文本向量检索 query（每条对应一个 target_role）。"""

from __future__ import annotations

from typing import Any

from backend.api_debug import log_keywords_extracted
from backend.intent.slot_defs import ROLE_EN_TO_SEARCH_KEYWORD
from backend.models import UserIntent


def _role_zh(role: str) -> str:
    key = (role or "").strip().lower()
    return ROLE_EN_TO_SEARCH_KEYWORD.get(key, (role or "").strip())


def _shared_intent_tokens(intent: UserIntent) -> list[str]:
    """gender、season、occasion_tags、style_tags（不含 color / text / category）。

    category 不放共享：它是 per-role 的（如裤子品类只属于 bottoms），拼进共享会
    污染其他 role 的 keyword（鞋 keyword 带上裤子品类）。per-role category 由
    ``_per_role_tokens`` 按 role 取。
    """
    tokens: list[str] = []
    if intent.gender:
        g = str(intent.gender).strip()
        if g:
            tokens.append(g)
    for s in intent.season or []:
        part = str(s).strip()
        if part:
            tokens.append(part)
    for tag in intent.occasion_tags or []:
        part = str(tag).strip()
        if part:
            tokens.append(part)
    for tag in intent.style_tags or []:
        part = str(tag).strip()
        if part:
            tokens.append(part)
    return tokens


# role → 默认品类词（无 per-role category 时补进 keyword）。
# text-vector 索引只 embed title，title 含品类词（如"梭织长裤"）但不含颜色，
# 故补品类词能 boost sim，补颜色无效（标题无颜色）。
_ROLE_DEFAULT_CATEGORY_KW: dict[str, str] = {
    "bottoms": "长裤",
    "shoes": "鞋",
    "top": "上衣",
    "dress": "连衣裙",
    "accessory": "配饰",
}


def _per_role_tokens(intent: UserIntent, role: str) -> list[str]:
    """该 role 专属 token：color/color_series/category。

    - color/color_series：来自 target_slots[role].positive（如 粉色/粉色系）。
    - category：target_slots[role].positive.category，无则按 role 默认品类词
      （bottoms→长裤 等）。

    text-vector 索引已把 color_name/color_series/season 编入嵌入文本，故 keyword
    带 color/color_series 能 boost sim（之前只 embed title 时加 color 无效）。
    顺序：品类在前，色在后（品类对 title 匹配权重更高）。
    """
    pn = (intent.target_slots or {}).get(role) or {}
    pos = pn.get("positive") or {}
    tokens: list[str] = []

    cats = pos.get("category") or []
    if isinstance(cats, str):
        cats = [cats]
    cats = [str(c).strip() for c in cats if str(c).strip()]
    if cats:
        tokens.extend(cats)
    else:
        default = _ROLE_DEFAULT_CATEGORY_KW.get((role or "").strip().lower())
        if default:
            tokens.append(default)

    colors = pos.get("color") or []
    if isinstance(colors, str):
        colors = [colors]
    for c in colors:
        c = str(c).strip()
        if c and c not in tokens:
            tokens.append(c)
    cs_list = pos.get("color_series") or []
    if isinstance(cs_list, str):
        cs_list = [cs_list]
    for cs in cs_list:
        cs = str(cs).strip()
        if cs and cs not in tokens:
            tokens.append(cs)
    return tokens


def build_query_for_target_role(
    role: str, shared: list[str], per_role_tokens: list[str] | None = None,
) -> str:
    """单条 query：品类中文 + 共享意图字段 + 该 role 专属 category，中文逗号连接。"""
    parts = [_role_zh(role)]
    parts.extend(shared)
    if per_role_tokens:
        parts.extend(per_role_tokens)
    return "，".join(p for p in parts if p)


def extract_query_keywords(
    intent: UserIntent,
    *,
    max_phrases: int | None = None,
    trace_id: str | None = None,
) -> list[str]:
    """仅依据意图识别结果：每个 ``target_roles`` 生成一条检索 query。

    示例（target_roles=bottoms,shoes；gender=女；occasion=日常,运动；
    style=简约,运动休闲）::

        下装，女，日常，运动，简约，运动休闲
        鞋，女，日常，运动，简约，运动休闲
    """
    cfg_max = 6
    try:
        from backend.config import load_config

        rec = load_config().get("recommend") or {}
        cfg_max = int(rec.get("text_keyword_max") or 6)
    except Exception:  # noqa: BLE001
        pass
    cap = max_phrases if max_phrases is not None else cfg_max

    shared = _shared_intent_tokens(intent)
    roles = [str(r).strip() for r in (intent.target_roles or []) if str(r).strip()]

    queries: list[str] = []
    if roles:
        for role in roles:
            q = build_query_for_target_role(role, shared, _per_role_tokens(intent, role))
            if q:
                queries.append(q)
    elif shared:
        # 无 target_roles 时退化为仅共享字段的一条 query
        queries.append("，".join(shared))

    result = queries[:cap]
    per_role: list[dict[str, Any]] = []
    for role in roles[:cap]:
        per_role.append(
            {
                "target_role": role,
                "role_zh": _role_zh(role),
                "query": build_query_for_target_role(role, shared, _per_role_tokens(intent, role)),
            },
        )
    sources: dict[str, Any] = {
        "query_type": intent.query_type,
        "target_roles": list(intent.target_roles or []),
        "gender": intent.gender,
        "season": list(intent.season or []),
        "occasion_tags": list(intent.occasion_tags or []),
        "style_tags": list(intent.style_tags or []),
        "shared_tokens": shared,
        "per_role_queries": per_role,
    }
    log_keywords_extracted(result, trace_id=trace_id, sources=sources)
    return result
