"""中类(category_l2)搭配规则：从 YAML 加载互补中类，供 ES/Milvus 召回过滤。"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

logger = logging.getLogger(__name__)

PairingListMode = Literal["primary", "allowed"]

_DICT_DIR = Path(__file__).resolve().parent / "dictionaries"

_SOURCE_FILE_MAP: dict[str, str] = {
    "cooccurrence": "category_l2_pairing.yaml",
    "cartesian": "category_l2_cartesian_pairing.yaml",
}


def _resolve_dict_path() -> Path:
    """根据 config 中的 category_l2_pairing_source 选择 YAML 数据源。"""
    from backend.config import load_config

    rec = load_config().get("recommend") or {}
    source = str(rec.get("category_l2_pairing_source") or "cooccurrence").strip().lower()
    filename = _SOURCE_FILE_MAP.get(source)
    if not filename:
        logger.warning(
            "unknown category_l2_pairing_source=%r, fallback to cooccurrence",
            source,
        )
        filename = _SOURCE_FILE_MAP["cooccurrence"]
    return _DICT_DIR / filename

_PAIRING_LIST_YAML_KEYS: dict[PairingListMode, str] = {
    "primary": "primary",
    "allowed": "allowed",
}

# ES/Milvus target_role → YAML category_meta.role（搭配角色）
_TARGET_ROLE_PAIRING_ROLES: dict[str, frozenset[str]] = {
    "top": frozenset({"上装", "外套"}),
    "bottoms": frozenset({"下装"}),
    "shoes": frozenset({"鞋"}),
    "accessory": frozenset({"配饰", "包"}),
    "dress": frozenset({"下装", "连衣裙"}),
}


@lru_cache(maxsize=1)
def _load_pairing_data() -> dict[str, Any]:
    dict_path = _resolve_dict_path()
    if not dict_path.is_file():
        logger.warning("category_l2 pairing rules not found: %s", dict_path)
        return {}
    with dict_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def get_pairing_list_mode() -> PairingListMode:
    """读取 config：primary（默认）或 allowed。"""
    from backend.config import load_config

    rec = load_config().get("recommend") or {}
    mode = str(rec.get("category_l2_pairing_list") or "primary").strip().lower()
    if mode in _PAIRING_LIST_YAML_KEYS:
        return mode  # type: ignore[return-value]
    logger.warning(
        "unknown category_l2_pairing_list=%r, fallback to primary",
        mode,
    )
    return "primary"


def _normalize_cat(s: str) -> str:
    """去除所有空白（含中间空格），用于模糊匹配 YAML key。"""
    return "".join(s.split())


def _get_confidence_thresholds() -> tuple[int, int]:
    """读取置信度阈值：(primary阈值, allowed阈值)。低于 allowed 阈值时不做中类过滤。"""
    from backend.config import load_config

    rec = load_config().get("recommend") or {}
    thresholds = rec.get("category_l2_pairing_confidence_thresholds") or {}
    primary = int(thresholds.get("primary_min_count") or 50)
    allowed = int(thresholds.get("allowed_min_count") or 10)
    return primary, allowed


def get_companion_categories(
    anchor_category_l2: str,
    *,
    list_mode: PairingListMode | None = None,
    adaptive: bool = True,
) -> list[str] | None:
    """锚点中类对应的互补中类列表（primary 或 allowed）。

    当 adaptive=True（默认）时，根据 anchor_as_master_count 动态选择：
      count >= primary_min_count → primary_companions
      count >= allowed_min_count → allowed_companions
      count < allowed_min_count  → None（不做中类过滤，仅按 role 过滤）
    """
    cat = (anchor_category_l2 or "").strip()
    if not cat:
        return None
    pairing_rules = _load_pairing_data().get("pairing_rules") or {}
    rules = pairing_rules.get(cat)
    if not isinstance(rules, dict):
        norm = _normalize_cat(cat)
        for key, val in pairing_rules.items():
            if _normalize_cat(key) == norm and isinstance(val, dict):
                rules = val
                break
    if not isinstance(rules, dict):
        return None

    if adaptive and list_mode is None:
        anchor_count = int(rules.get("anchor_count") or 0)
        primary_min, allowed_min = _get_confidence_thresholds()
        if anchor_count >= primary_min:
            effective_mode: PairingListMode = "primary"
        elif anchor_count >= allowed_min:
            effective_mode = "allowed"
        else:
            logger.info(
                "category_l2 adaptive: %r anchor_count=%d < %d, "
                "skipping category_l2 filter (low confidence)",
                cat, anchor_count, allowed_min,
            )
            return None
        logger.debug(
            "category_l2 adaptive: %r anchor_count=%d → mode=%s",
            cat, anchor_count, effective_mode,
        )
    else:
        effective_mode = list_mode or get_pairing_list_mode()

    yaml_key = _PAIRING_LIST_YAML_KEYS[effective_mode]
    raw = rules.get(yaml_key) or []
    out = [str(x).strip() for x in raw if str(x).strip()]
    return out or None


def get_allowed_companions(anchor_category_l2: str) -> list[str] | None:
    """兼容旧名：等同 get_companion_categories（受 config 控制）。"""
    return get_companion_categories(anchor_category_l2)


@lru_cache(maxsize=1)
def _category_l2_role_map() -> dict[str, str]:
    """中类 -> 搭配角色（上装/下装/外套/鞋/配饰）。"""
    rules = _load_pairing_data().get("pairing_rules") or {}
    out: dict[str, str] = {}
    if not isinstance(rules, dict):
        return out
    for cat, info in rules.items():
        if not isinstance(info, dict):
            continue
        role = str(info.get("role") or "").strip()
        if role:
            out[str(cat)] = role
    return out


def filter_companions_for_target_role(
    companions: list[str] | None,
    target_role: str,
) -> list[str] | None:
    """将互补中类列表收窄为与 target_role 匹配的中类。"""
    if not companions:
        return None
    role_key = (target_role or "").strip().lower()
    accepted = _TARGET_ROLE_PAIRING_ROLES.get(role_key)
    if not accepted:
        return list(companions)
    role_map = _category_l2_role_map()
    filtered = [
        cat for cat in companions
        if role_map.get(cat, "") in accepted
    ]
    return filtered or None


# 兼容旧函数名
filter_allowed_companions_for_target_role = filter_companions_for_target_role


def build_category_l2_es_filter(categories: list[str]) -> dict[str, Any] | None:
    """构造 ES category_l2 白名单 filter。"""
    cats = [str(c).strip() for c in categories if str(c).strip()]
    if not cats:
        return None
    if len(cats) == 1:
        return {"term": {"category_l2": cats[0]}}
    return {"terms": {"category_l2": cats}}


def build_category_l2_milvus_expr(categories: list[str]) -> str | None:
    """构造 Milvus expr：category_l2 in [...]。"""
    cats = [str(c).strip() for c in categories if str(c).strip()]
    if not cats:
        return None
    quoted = ", ".join(f'"{c}"' for c in cats)
    return f"category_l2 in [{quoted}]"


def merge_milvus_expr(*parts: str | None) -> str | None:
    """合并多个 Milvus 布尔表达式。"""
    valid = [p.strip() for p in parts if p and p.strip()]
    if not valid:
        return None
    return " and ".join(valid)


def build_group_brand_milvus_expr(group_brand: str | None) -> str | None:
    """构造 Milvus expr：group_brand == "..."。

    集团品牌为单值 VARCHAR 枚举（斐乐大货/斐乐儿童/斐乐潮牌/斐乐专业运动），
    非空时等值过滤；空则不限制（保持现有召回行为）。
    """
    gb = (group_brand or "").strip()
    if not gb:
        return None
    return f'group_brand == "{gb}"'


def _resolve_anchor_category_l2(anchor_row: dict[str, Any] | None) -> str:
    """锚点 category_l2：虚拟图锚点也由意图模块融合产出 category_l2，
    故统一直接读 anchor_row.category_l2，不再对 _is_virtual_image_anchor 短路。"""
    if not anchor_row:
        return ""
    return str(anchor_row.get("category_l2") or "").strip()


def resolve_pairing_companions(
    anchor_row: dict[str, Any] | None,
    *,
    intent_categories: list[str] | None = None,
    list_mode: PairingListMode | None = None,
    intent_override_categories: list[str] | None = None,
) -> tuple[list[str] | None, str]:
    """解析互补中类：优先锚点 SKU 中类，其次意图 category 槽位。

    Args:
        intent_override_categories: 用户明确指定的品类（如"帮我配双户外鞋"），
            无条件加入白名单，不受 forbidden 限制。

    Returns:
        (companions, anchor_category_l2)
    """
    mode = list_mode or get_pairing_list_mode()
    cat2 = _resolve_anchor_category_l2(anchor_row)
    if cat2:
        companions = get_companion_categories(cat2, list_mode=mode)
        if companions:
            companions = _merge_intent_overrides(companions, intent_override_categories)
            return companions, cat2
    for raw in intent_categories or []:
        cat = str(raw).strip()
        if not cat:
            continue
        companions = get_companion_categories(cat, list_mode=mode)
        if companions:
            logger.debug(
                "category_l2 pairing from intent.category fallback: %s",
                cat,
            )
            companions = _merge_intent_overrides(companions, intent_override_categories)
            return companions, cat
    return None, cat2


def _merge_intent_overrides(
    companions: list[str],
    overrides: list[str] | None,
) -> list[str]:
    """将用户明确指定的品类合并进白名单（去重，保留原有顺序）。"""
    if not overrides:
        return companions
    existing = set(companions)
    merged = list(companions)
    for cat in overrides:
        cat = str(cat).strip()
        if cat and cat not in existing:
            merged.append(cat)
            existing.add(cat)
            logger.info("intent_override: added %r to companions whitelist", cat)
    return merged


def resolve_pairing_allowed_companions(
    anchor_row: dict[str, Any] | None,
    *,
    intent_categories: list[str] | None = None,
    intent_override_categories: list[str] | None = None,
) -> list[str] | None:
    """兼容旧 API：返回互补中类列表（受 config 控制 primary/allowed）。"""
    companions, _ = resolve_pairing_companions(
        anchor_row,
        intent_categories=intent_categories,
        intent_override_categories=intent_override_categories,
    )
    return companions


def resolve_anchor_category_l2_filter(
    anchor_row: dict[str, Any] | None,
) -> list[str] | None:
    """从锚点 SKU 行解析互补中类；虚拟锚点或无中类时跳过。"""
    cat2 = _resolve_anchor_category_l2(anchor_row)
    if not cat2:
        return None
    return get_companion_categories(cat2)
