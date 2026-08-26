"""色系(color_series)搭配规则：从 YAML 加载互补色系，供 ES/Milvus 召回过滤。"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

logger = logging.getLogger(__name__)

PairingListMode = Literal["primary", "allowed"]
ColorSeriesMatchMode = Literal["strict", "relaxed", "auto"]

_DICT_DIR = Path(__file__).resolve().parent / "dictionaries"
_DICT_PATH = _DICT_DIR / "color_series_pairing.yaml"

# 方向化色系配对 YAML：上装↔下装 / 下装↔鞋 的正向与反向统计来源不同
# (上装→下装 与 下装→上装 的色系分布并不对称，见 scripts/extract_fila_sku_color_pairing.py)。
# (anchor_role, companion_role) → YAML 文件名；未命中的方向回退到对称的 _DICT_PATH。
_DIRECTIONAL_PAIRING_YAML: dict[tuple[str, str], str] = {
    ("上装", "下装"): "fila_sku_color_pairing_top_bottom.yaml",
    ("下装", "上装"): "fila_sku_color_pairing_bottom_top.yaml",
    ("下装", "鞋"): "fila_sku_color_pairing_bottom_shoe.yaml",
}

_PAIRING_LIST_YAML_KEYS: dict[PairingListMode, str] = {
    "primary": "primary_companions",
    "allowed": "allowed_companions",
}

# 抽象色系 → 具体色系展开映射
_ABSTRACT_COLOR_SERIES: dict[str, list[str]] = {
    "暖色系": ["红色系", "橙色系", "黄色系", "棕色系"],
    "冷色系": ["蓝色系", "绿色系", "紫色系"],
    "中性色系": ["黑色系", "白色系", "灰色系", "米色系"],
    "黑白灰色系": ["黑色系", "白色系", "灰色系", "米色系"],
    "大地色系": ["棕色系", "黄色系", "橙色系"],
    "莫兰迪色系": ["灰色系", "粉色系", "棕色系", "绿色系"],
    "深色系": ["黑色系", "蓝色系", "棕色系", "紫色系"],
    "浅色系": ["白色系", "粉色系", "黄色系"],
    "亮色系": ["白色系", "粉色系", "黄色系"],
    "糖果色系": ["粉色系", "黄色系", "紫色系", "绿色系"],
    "马卡龙色系": ["粉色系", "黄色系", "紫色系", "绿色系"],
}

# 撞色：每个色系对应的对比色系
_CONTRAST_COLOR_SERIES: dict[str, list[str]] = {
    "红色系": ["绿色系", "蓝色系"],
    "橙色系": ["蓝色系", "紫色系"],
    "黄色系": ["紫色系", "蓝色系"],
    "绿色系": ["红色系", "粉色系"],
    "蓝色系": ["橙色系", "黄色系"],
    "紫色系": ["黄色系", "橙色系"],
    "粉色系": ["绿色系", "蓝色系"],
    "棕色系": ["蓝色系", "绿色系"],
    "黑色系": ["白色系", "红色系"],
    "白色系": ["黑色系", "红色系"],
    "灰色系": ["红色系", "蓝色系"],
}


def expand_abstract_color_series(
    color_series: str,
    *,
    anchor_color_series: str = "",
) -> list[str] | None:
    """将抽象色系展开为具体色系列表。

    - 同色系 → 返回锚点自身色系
    - 撞色系 → 返回锚点的对比色系
    - 其他抽象色系 → 查映射表展开
    - 非抽象色系 → 返回 None（交给常规搭配规则处理）
    """
    cs = (color_series or "").strip()
    if not cs:
        return None
    if cs == "同色系":
        anchor = (anchor_color_series or "").strip()
        return [anchor] if anchor else None
    if cs == "撞色系":
        anchor = (anchor_color_series or "").strip()
        if anchor and anchor in _CONTRAST_COLOR_SERIES:
            return _CONTRAST_COLOR_SERIES[anchor]
        return None
    if cs in _ABSTRACT_COLOR_SERIES:
        return list(_ABSTRACT_COLOR_SERIES[cs])
    return None


@lru_cache(maxsize=None)
def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        logger.warning("color_series pairing rules not found: %s", path)
        return {}
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


@lru_cache(maxsize=1)
def _load_pairing_data() -> dict[str, Any]:
    return _load_yaml(_DICT_PATH)


def _directional_enabled() -> bool:
    """读取 config：是否启用方向化色系配对 YAML（默认开启）。"""
    from backend.config import load_config

    rec = load_config().get("recommend") or {}
    return bool(rec.get("enable_directional_color_series_pairing", True))


def _select_pairing_data(
    anchor_role: str,
    companion_role: str,
) -> dict[str, Any]:
    """按 (anchor_role, companion_role) 选方向化 YAML；未命中或禁用时回退对称规则。"""
    if anchor_role and companion_role and _directional_enabled():
        yaml_name = _DIRECTIONAL_PAIRING_YAML.get((anchor_role, companion_role))
        if yaml_name:
            return _load_yaml(_DICT_DIR / yaml_name)
    return _load_pairing_data()


def get_pairing_list_mode() -> PairingListMode:
    """读取 config：primary（默认）或 allowed。"""
    from backend.config import load_config

    rec = load_config().get("recommend") or {}
    mode = str(rec.get("color_series_pairing_list") or "primary").strip().lower()
    if mode in _PAIRING_LIST_YAML_KEYS:
        return mode  # type: ignore[return-value]
    return "primary"


def _get_cs_confidence_thresholds() -> tuple[int, int]:
    """色系搭配置信度阈值：(primary_min_count, allowed_min_count)。"""
    from backend.config import load_config

    rec = load_config().get("recommend") or {}
    thresholds = rec.get("color_series_pairing_confidence_thresholds") or {}
    primary = int(thresholds.get("primary_min_count") or 200)
    allowed = int(thresholds.get("allowed_min_count") or 50)
    return primary, allowed


def _resolve_effective_cs_mode(
    anchor_color_series: str,
    rules: dict[str, Any],
) -> PairingListMode | None:
    """根据 anchor_count 自适应选择 pairing list mode。

    anchor_count >= primary_min_count → primary
    anchor_count >= allowed_min_count → allowed
    anchor_count < allowed_min_count  → None（不做色系过滤）
    """
    anchor_count = int(rules.get("anchor_count") or 0)
    primary_min, allowed_min = _get_cs_confidence_thresholds()
    if anchor_count >= primary_min:
        effective_mode: PairingListMode = "primary"
    elif anchor_count >= allowed_min:
        effective_mode = "allowed"
    else:
        logger.info(
            "color_series adaptive: %r anchor_count=%d < %d, "
            "skip color_series pairing filter",
            anchor_color_series, anchor_count, allowed_min,
        )
        return None
    logger.info(
        "color_series adaptive: %r anchor_count=%d → mode=%s",
        anchor_color_series, anchor_count, effective_mode,
    )
    return effective_mode


def get_companion_color_series(
    anchor_color_series: str,
    *,
    list_mode: PairingListMode | None = None,
    anchor_role: str = "",
    companion_role: str = "",
) -> list[str] | None:
    """锚点色系对应的互补色系列表（primary 或 allowed）。

    支持置信度自适应：小样本色系自动降级或跳过过滤。
    多色系作为锚点时不做色系过滤。

    方向化配对：传入 ``anchor_role``/``companion_role`` 时，按方向选
    fila_sku 方向化 YAML（上装→下装 与 下装→上装 统计不对称，需分别取用）；
    未命中的方向回退到对称的 color_series_pairing.yaml。
    """
    cs = (anchor_color_series or "").strip()
    if not cs:
        return None

    # 多色系锚点不做色系过滤
    if cs == "多色系":
        return None

    # 抽象色系先展开（暖色系、冷色系等）
    expanded = expand_abstract_color_series(cs)
    if expanded is not None:
        return expanded if expanded else None

    pairing_data = _select_pairing_data(anchor_role, companion_role)
    rules = (pairing_data.get("pairing_rules") or {}).get(cs)
    if not isinstance(rules, dict):
        return None

    # 置信度自适应：小样本色系降级或跳过
    if list_mode is None:
        effective_mode = _resolve_effective_cs_mode(cs, rules)
        if effective_mode is None:
            return None
    else:
        effective_mode = list_mode

    yaml_key = _PAIRING_LIST_YAML_KEYS[effective_mode]
    raw = rules.get(yaml_key) or []
    out = [str(x).strip() for x in raw if str(x).strip()]
    # 同色系搭配：锚点自身色系也应允许
    if cs not in out:
        out.append(cs)
    return out or None


def build_color_series_es_filter(
    color_series_list: list[str] | None,
    *,
    mode: ColorSeriesMatchMode = "relaxed",
) -> dict[str, Any] | None:
    """构造 ES color_series 白名单 filter。

    - relaxed：color_series (多值 keyword) 与白名单有交集即命中。
    - strict：额外要求 color_series_count == 1（纯色 SKU 才严格匹配）。
    - auto：由调用方先 strict 再 relaxed 两段查询，本函数不直接处理；按 relaxed 返回。
    """
    if not color_series_list:
        return None
    cats = [str(c).strip() for c in color_series_list if str(c).strip()]
    if not cats:
        return None
    terms_clause: dict[str, Any]
    if len(cats) == 1:
        terms_clause = {"term": {"color_series": cats[0]}}
    else:
        terms_clause = {"terms": {"color_series": cats}}
    if mode == "strict":
        # 纯色 SKU：color_series 数组长度恰好 1
        return {
            "bool": {
                "must": [
                    terms_clause,
                    {"term": {"color_series_count": 1}},
                ],
            },
        }
    return terms_clause


def build_color_series_milvus_expr(
    color_series_list: list[str] | None,
    *,
    mode: ColorSeriesMatchMode = "relaxed",
) -> str | None:
    """构造 Milvus expr：color_series (ARRAY) 与白名单的匹配。

    - relaxed：array_contains_any(color_series, [...]) —— 多色 SKU 任一命中即召回。
    - strict：array_length(color_series) == 1 && array_contains_any(color_series, [...])
              —— 仅纯色 SKU 命中。
    - auto：由调用方先 strict 再 relaxed 两段查询；本函数按 relaxed 返回。
    """
    if not color_series_list:
        return None
    cats = [str(c).strip() for c in color_series_list if str(c).strip()]
    if not cats:
        return None
    if "多色系" not in cats:
        cats.append("多色系")
    quoted = ", ".join(f'"{c}"' for c in cats)
    contains = f"array_contains_any(color_series, [{quoted}])"
    if mode == "strict":
        return f"array_length(color_series) == 1 && {contains}"
    return contains


def resolve_anchor_color_series(
    anchor_row: dict[str, Any] | None,
) -> str:
    """从锚点 SKU 行获取主色系（喂给 pairing 规则）；虚拟锚点/多色锚点跳过。

    color_series 现为数组：纯色（长度 1 且非"多色系"）→ 返回该单值；
    多色组合或印花（长度 >1 或含"多色系"）→ 返回空串，由 get_companion 跳过色系过滤。
    """
    if not anchor_row or anchor_row.get("_is_virtual_image_anchor"):
        return ""
    raw = anchor_row.get("color_series")
    if isinstance(raw, list):
        vals = [str(x).strip() for x in raw if str(x).strip()]
    elif isinstance(raw, str) and raw.strip():
        vals = [raw.strip()]
    else:
        return ""
    if len(vals) != 1 or vals[0] == "多色系":
        return ""
    return vals[0]
