"""per-target-role 槽位合并工具。

将意图解析的扁平全局槽位与 ``target_slots[role]``（含 ``positive`` 正向覆盖与
``negative`` 否定）合并，供召回层按角色消费。

- ``effective_role_slots``：全局 flat 槽位 ← ``target_slots[role].positive`` 覆盖，
  返回该 role 生效的正向约束。
- ``role_negative_slots``：``target_slots["*"].negative`` ∪
  ``target_slots[role].negative``，按 slot 聚合，返回该 role 生效的否定约束。

覆盖字段集：``color_series / category / length_class / coverage / scene_domain / series``
（Milvus complementary 集合已索引，见 ``scripts/build_complementary_vectors.py``）。
``color``（具体色名）非 Milvus 字段，仅在 ES 文本侧使用，这里仍一并返回供下游自取。
``series``（子品牌线/联名胶囊）与 ``scene_domain`` 同为标量结构属性：顶层
``intent.series`` 是锚点权威单值，**不**作为 target 默认继承（否则会把锚点系列
强制塞给所有 role），仅当 ``target_slots[role].positive.series`` 显式给出时才生效。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Optional

from backend.models import UserIntent
from backend.intent.sku_attributes import expand_modeling

# 可被 per-role 覆盖的标量结构化属性（单值，override 整体替换）
# series 与 scene_domain 同为"锚点描述性、不从顶层继承"的标量结构槽：顶层
# intent.series 是锚点系列，推给所有 role 会强制同系；per-role 显式给出时才覆盖。
_SCALAR_ATTR_SLOTS = ("length_class", "coverage", "scene_domain", "series", "modeling")
# 可被 per-role 覆盖的列表槽位（多值，override 整体替换而非合并）
_LIST_SLOTS = ("color", "color_series", "category", "style_tags", "occasion_tags")
# 可下推到 Milvus expr 的结构化字段（标量 + 列表子集）
_MILVUS_FILTER_SLOTS = ("color_series", "category", "length_class", "coverage", "scene_domain", "series", "modeling")


def effective_role_slots(intent: UserIntent, role: str) -> dict[str, Any]:
    """全局 flat 槽位 ← ``target_slots[role]`` 覆盖，返回该 role 生效的正向约束。

    override 语义：role 在 target_slots 中出现的 slot 整体替换全局默认；未出现则
    沿用顶层 flat 值。

    ⚠️ ``length_class/coverage/scene_domain/series`` 四个标量结构化属性在 prompt
    （第十四/十五节）中**描述锚点单品**，不作为 target 默认继承——否则会把锚点
    袖长/覆盖/场景/系列误塞给 target（如锚点上装长袖 → 强制下装 long；锚点 GOLF
    系列 → 强制全 role 同系）。这四个字段仅当 ``target_slots[role]`` 显式给出时才
    作为 target 约束；其余列表槽位（color/color_series/category/style_tags/occasion_tags）
    是用户通用偏好，正常继承。
    """
    base: dict[str, Any] = {
        "color": list(intent.color or []),
        # color_series 不从全局 intent.color_series 继承：intent.color_series 是锚点颜色
        # （如 白色系），推到其他 role 会变成 color_series in [白色系] 硬过滤，导致
        # 锚点颜色无对应款时 0 召回（如白色锚点 + 男秋 bottoms 白色系=0）。
        # per-role color_series 由 target_slots[role].positive.color_series 显式给出；
        # 无 per-role 时由调用方（complementary 通路）用锚点颜色的搭配色系兜底。
        "color_series": [],
        # category 不从全局 intent.category 继承：intent.category 是锚点品类（如 短袖编织衫），
        # 推到其他 role 会变成 category_l2 in [短袖编织衫] 导致 bottoms/shoes 零召回。
        # per-role category 仅由 target_slots[role].positive.category 显式给出。
        "category": [],
        # 锚点描述性标量：不继承，仅 target_slots 显式给出时生效
        "length_class": None,
        "coverage": None,
        "scene_domain": None,
        # series 不从顶层 intent.series 继承：intent.series 是锚点系列（如 GOLF），
        # 推到所有 role 会强制全 role 同系，扼杀跨系列自由搭配。per-role series 仅由
        # target_slots[role].positive.series 显式给出；无 per-role 时由调用方用锚点
        # 同系隔离（build_series_es/milvus_expr）兜底。
        "series": None,
        # 版型是用户对单品的偏好（非锚点描述），从全局 intent.modeling 继承，
        # per-role 显式给出时整体替换。同义词归并在召回侧 expand_modeling 展开。
        "modeling": intent.modeling or None,
        # 价格区间：全局 intent.budget_min/max 继承，per-role 显式给出时替换。
        "budget_min": intent.budget_min,
        "budget_max": intent.budget_max,
        "style_tags": list(intent.style_tags or []),
        "occasion_tags": list(intent.occasion_tags or []),
    }
    override = ((intent.target_slots or {}).get(role) or {}).get("positive") or {}
    if not isinstance(override, dict):
        return base
    for slot, val in override.items():
        if val is None:
            continue
        if slot in _SCALAR_ATTR_SLOTS:
            s = str(val).strip()
            if s:
                base[slot] = s
        elif slot in _LIST_SLOTS:
            if isinstance(val, list):
                cleaned = [str(x).strip() for x in val if str(x).strip()]
                if cleaned:
                    base[slot] = cleaned
            elif isinstance(val, str) and val.strip():
                base[slot] = [val.strip()]
        elif slot in ("budget_min", "budget_max"):
            # 数值槽位：float 强转，非正数视为未指定（不覆盖全局）
            try:
                fval = float(val)
            except (TypeError, ValueError):
                continue
            if fval > 0:
                base[slot] = fval
    return base


def effective_role_budget(
    intent: UserIntent, role: str
) -> tuple[Optional[float], Optional[float]]:
    """该 role 生效的 (budget_min, budget_max)，均可能为 None。

    全局 intent.budget_min/max ← target_slots[role].positive 同名覆盖。
    """
    slots = effective_role_slots(intent, role)
    return slots.get("budget_min"), slots.get("budget_max")


def role_negative_slots(intent: UserIntent, role: str) -> dict[str, list[str]]:
    """``target_slots["*"].negative`` ∪ ``target_slots[role].negative``，按 slot 聚合去重。

    返回 ``{slot: [values]}``，仅含下推友好的结构化 slot（color_series/category/
    length_class/coverage/scene_domain/series/color）。
    """
    neg = intent.target_slots or {}
    merged: dict[str, list[str]] = defaultdict(list)
    seen: dict[str, set[str]] = defaultdict(set)
    for key in ("*", role):
        slot_map = ((neg.get(key) or {}).get("negative") or {})
        if not isinstance(slot_map, dict):
            continue
        for slot, vals in slot_map.items():
            if slot not in _LIST_SLOTS and slot not in _SCALAR_ATTR_SLOTS:
                continue
            if isinstance(vals, str):
                vals = [vals]
            if not isinstance(vals, list):
                continue
            for v in vals:
                s = str(v).strip()
                if not s or s in seen[slot]:
                    continue
                seen[slot].add(s)
                merged[slot].append(s)
    return dict(merged)


def milvus_filterable_positive(slots: dict[str, Any]) -> dict[str, Any]:
    """从 effective_role_slots 结果中筛出可下推 Milvus expr 的正向字段。"""
    return {k: v for k, v in slots.items() if k in _MILVUS_FILTER_SLOTS and v}


def milvus_filterable_negative(neg: dict[str, list[str]]) -> dict[str, list[str]]:
    """从 role_negative_slots 结果中筛出可下推 Milvus expr 的否定字段。"""
    return {k: v for k, v in neg.items() if k in _MILVUS_FILTER_SLOTS and v}


def _milvus_escape(s: Any) -> str:
    """转义 Milvus expr 字符串字面量中的反斜杠与双引号。"""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _milvus_in_clause(field: str, values: list[str]) -> str:
    quoted = ",".join(f'"{_milvus_escape(v)}"' for v in values)
    # color_series 是 ARRAY 字段（commit 3167ec8 多值数组改造）：
    # 标量 ``field in [...]`` 会让 Milvus 报 code=1100 “cannot be casted to Array”，
    # 整条 expr 解析失败 → 该 role 0 召回。Array 字段改用 array_contains_any。
    if field in _ARRAY_MILVUS_FIELDS:
        return f"array_contains_any({field}, [{quoted}])"
    return f"{field} in [{quoted}]"


def _milvus_not_in_clause(field: str, values: list[str]) -> str:
    quoted = ",".join(f'"{_milvus_escape(v)}"' for v in values)
    if field in _ARRAY_MILVUS_FIELDS:
        return f"not array_contains_any({field}, [{quoted}])"
    return f"{field} not in [{quoted}]"


# slot → Milvus 实际字段名（category 在 Milvus 集合里叫 category_l2）
_MILVUS_FIELD_NAME = {
    "color_series": "color_series",
    "category": "category_l2",
    "length_class": "length_class",
    "coverage": "coverage",
    "scene_domain": "scene_domain",
    "series": "series",
    "modeling": "modeling",
}
_SCALAR_MILVUS_SLOTS = ("length_class", "coverage", "scene_domain", "series")
_LIST_MILVUS_SLOTS = ("color_series", "category")
# 在 Milvus 集合中为 ARRAY 字段的实际字段名（需用 array_contains_any，见上）
_ARRAY_MILVUS_FIELDS = {"color_series"}


def _override_slots(intent: UserIntent, role: str) -> dict[str, Any]:
    """仅 ``target_slots[role]`` 中可下推的字段（不含全局默认）。

    用于通路2/3（文本向量 / ES）：这些通路已通过 pairing cs_filter / cat2_filter
    处理全局色系与品类，故 per-role 注入只应推「用户为该 role 显式指定的覆盖」，
    避免与 pairing 的全局展开 AND 后产生空集。
    """
    ov = ((intent.target_slots or {}).get(role) or {}).get("positive") or {}
    if not isinstance(ov, dict):
        return {}
    return {k: v for k, v in ov.items() if k in _MILVUS_FILTER_SLOTS and v}


def build_role_milvus_expr_parts(
    intent: UserIntent, role: str, *,
    include_global: bool = True, skip_slots: set[str] | None = None,
) -> list[str]:
    """构建某 target_role 的 per-role 正向/否定 Milvus expr 片段列表。

    正向：``array_contains_any(color_series, [...])``（ARRAY）、
    ``category_l2 in [...]``、``length_class == "x"``、``coverage == "x"``、
    ``scene_domain == "x"``、``series == "x"``。
    否定：对应 ``not array_contains_any(...)`` / ``not in [...]``。中性值（n/a/空）跳过。
    调用方将各片段并入 ``_build_role_milvus_expr`` 的 AND 链（与锚点冲突规则并联，不替换）。

    ``include_global=True``（默认，complementary 通路）：正向取 effective（全局默认
    ← per-role 覆盖），因该通路无 pairing cs_filter，全局色系只能由此推。
    ``include_global=False``（文本向量通路）：正向只取 per-role 覆盖，全局色系/品类
    由 pairing filter 负责，避免重复 AND 致空。

    ``skip_slots``：progressive relax 丢弃的 slot 名集合，正向/否定对应项跳过。
    """
    skip = skip_slots or set()
    parts: list[str] = []
    pos_src = effective_role_slots(intent, role) if include_global else _override_slots(intent, role)
    pos = milvus_filterable_positive(pos_src)
    neg = milvus_filterable_negative(role_negative_slots(intent, role))

    for slot in _LIST_MILVUS_SLOTS:
        if slot in skip:
            continue
        vals = pos.get(slot)
        if vals:
            parts.append(_milvus_in_clause(_MILVUS_FIELD_NAME[slot], list(vals)))
    for slot in _SCALAR_MILVUS_SLOTS:
        if slot in skip:
            continue
        v = pos.get(slot)
        if v and v not in ("n/a", ""):
            parts.append(f'{_MILVUS_FIELD_NAME[slot]} == "{_milvus_escape(v)}"')

    for slot in _LIST_MILVUS_SLOTS:
        if slot in skip:
            continue
        vals = neg.get(slot)
        if vals:
            parts.append(_milvus_not_in_clause(_MILVUS_FIELD_NAME[slot], list(vals)))
    for slot in _SCALAR_MILVUS_SLOTS:
        if slot in skip:
            continue
        vals = neg.get(slot)
        if vals:
            parts.append(_milvus_not_in_clause(_MILVUS_FIELD_NAME[slot], list(vals)))
    # 版型否定：多值各自同义词展开后并集 not in
    if "modeling" not in skip:
        m_neg = neg.get("modeling")
        if m_neg:
            union: list[str] = []
            seen: set[str] = set()
            for v in m_neg:
                for e in expand_modeling(str(v)):
                    if e not in seen:
                        seen.add(e)
                        union.append(e)
            if union:
                parts.append(_milvus_not_in_clause("modeling", union))

    return parts


def build_modeling_price_milvus_expr(
    intent: UserIntent, role: str, *,
    skip_modeling: bool = False, skip_price: bool = False,
) -> str | None:
    """该 role 生效的版型 + 价格区间 Milvus expr 片段，无约束返回 None。

    版型与价格均取 effective 值（全局 intent ← target_slots[role].positive 覆盖），
    与 ``build_role_milvus_expr_parts`` 的 color_series/category/length_class 解耦，
    避免与 include_global 的色系/品类继承策略耦合。供 text_vector / complementary
    通路在拼装 attr_expr 时并入。

    - modeling：``modeling in [同义词展开]``（宽松→{宽松,超宽松}）
    - price：``price >= min``、``price <= max``
    - ``skip_modeling`` / ``skip_price``：progressive relax 时跳过对应部分。
    """
    parts: list[str] = []
    eff = effective_role_slots(intent, role)
    if not skip_modeling:
        m = eff.get("modeling")
        if m and m not in ("n/a", ""):
            expanded = expand_modeling(str(m))
            if expanded:
                parts.append(_milvus_in_clause("modeling", expanded))
    if not skip_price:
        bmin, bmax = effective_role_budget(intent, role)
        if bmin and bmin > 0:
            parts.append(f"price >= {float(bmin)}")
        if bmax and bmax > 0:
            parts.append(f"price <= {float(bmax)}")
    if not parts:
        return None
    from backend.intent.category_l2_pairing import merge_milvus_expr
    return merge_milvus_expr(*parts)


def per_role_scene_domain(intent: UserIntent, role: str) -> str | None:
    """用户为该 role 显式指定的 scene_domain（已枚举归一），无则 None。

    用于覆盖锚点 scene_domain 隔离规则：用户明确指定场景时，以用户值为准。
    """
    v = (((intent.target_slots or {}).get(role) or {}).get("positive") or {}).get("scene_domain")
    if not isinstance(v, str):
        return None
    v = v.strip()
    return v if v and v not in ("n/a", "") else None


def per_role_series(intent: UserIntent, role: str) -> str | None:
    """用户为该 role 显式指定的 series（子品牌线，已 normalize_series 归一），无则 None。

    用于覆盖锚点同系隔离规则：用户明确为某 role 指定系列时（如 上衣 GOLF、鞋
    FILA FUSION LIFE），以用户值为准，对该 role 的锚点同系隔离与 ``_series_conflict``
    安全网让路（``role_has_explicit_positive`` 命中 → ``bypass_all``）。
    """
    v = (((intent.target_slots or {}).get(role) or {}).get("positive") or {}).get("series")
    if not isinstance(v, str):
        return None
    v = v.strip()
    return v if v and v not in ("n/a", "") else None


def _positive_val_nonempty(v: Any) -> bool:
    """positive 槽位值是否非空（str 去空白 / list-tuple-set 任一非空 / 标量非 None）。"""
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip() != ""
    if isinstance(v, (list, tuple, set)):
        return any(str(x).strip() for x in v)
    return True  # 数值等标量


def role_has_explicit_positive(intent: UserIntent, role: str) -> bool:
    """用户是否为该 target_role 在 ``target_slots[role].positive`` 显式设定了任一槽位。

    命中即表示用户对该 target_role 有**明确显式意图**，后续**所有锚点驱动的预过滤
    与冲突检测一律让路**——用户意图优先于锚点假设。具体让路项：
      - 预过滤下推：scene_domain 隔离、series 同系隔离、length/coverage/layer 结构子句、
        ``is_intimate == "false"``（贴身内衣隔离）、category pairing 中类白名单、
        color_series pairing 色系配对——一律不在该 role 下推（只在 per-role 正向值
        缺失且非 bypass 时才回退 pairing/结构隔离）。
      - 成对安全网：``check_companion_conflict`` / ``check_outfit_conflict`` 的全部
        YAML 规则 + scene 派生规则 + 内联 ``_series_conflict``——``bypass_all`` 早返回放行。
    positive 槽位含
    ``color/color_series/category/length_class/coverage/scene_domain/series/modeling/
    budget_min/budget_max/style_tags/occasion_tags``，任一非空即 True。

    安全性：positive 已在 ES/Milvus 正向过滤（``build_role_es_positive_filters`` /
    ``build_role_milvus_expr_parts``）与 ``_item_violates_intent`` 兜底中强制候选
    符合用户值，故锚点驱动的结构/冲突规则让路不会放行不符用户意图的单品——
    它们只会冗余或反杀用户要的款（如长袖锚点 × 用户要的短裤、daily 锚点 ×
    唯一的 golf 白色长裤、跨系列锚点 × 用户指定的同款异系列下装）。

    不让路的（非锚点驱动、本身就是用户意图的体现）：``gender_conflict`` /
    ``season_conflict`` / ``age_conflict``（由 ``intent.gender/season/age`` 驱动，
    是用户自报意图字段，非锚点假设该让路）。
    """
    pos = ((intent.target_slots or {}).get(role) or {}).get("positive") or {}
    if not isinstance(pos, dict) or not pos:
        return False
    return any(_positive_val_nonempty(v) for v in pos.values())


def per_role_color_series(intent: UserIntent, role: str) -> list[str]:
    """用户为该 role 显式指定的 color_series，无则空。用于覆盖 pairing cs_filter。"""
    v = (((intent.target_slots or {}).get(role) or {}).get("positive") or {}).get("color_series")
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if str(x).strip()]


def per_role_category(intent: UserIntent, role: str) -> list[str]:
    """用户为该 role 显式指定的 category（中类），无则空。用于覆盖 pairing cat2_filter。"""
    v = (((intent.target_slots or {}).get(role) or {}).get("positive") or {}).get("category")
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if str(x).strip()]


# ── ES 侧 per-role 注入（通路3 resolve_es_query_for_role） ──────────────────

_ES_FIELD_NAME = {
    "color_series": "color_series",
    "category": "category_l2",
    "length_class": "length_class",
    "coverage": "coverage",
    "scene_domain": "scene_domain",
    "series": "series",
    "modeling": "modeling",
}


def _es_term(field: str, value: str) -> dict[str, Any]:
    return {"term": {field: value}}


def _es_terms(field: str, values: list[str]) -> dict[str, Any]:
    return {"terms": {field: list(values)}}


def build_role_es_positive_filters(
    intent: UserIntent,
    role: str,
    *,
    exclude_slots: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """该 role 的 per-role 正向覆盖（仅 target_slots[role]，不含全局）→ ES bool.filter。

    全局色系/品类由 pairing cs_filter/cat2_filter 负责，此处只推用户显式 per-role 覆盖。

    ``exclude_slots``：在通路3（``resolve_es_query_for_role``）中，``color_series`` 与
    ``category`` 已由 ``cs_filter``/``cat2_filter``（含 per-role 覆盖）统一处理，传入
    这两个 slot 名以避免重复 AND。其他通路默认空，保留色系/品类注入。
    """
    pos = milvus_filterable_positive(_override_slots(intent, role))
    filters: list[dict[str, Any]] = []
    for slot in _LIST_MILVUS_SLOTS:
        if slot in exclude_slots:
            continue
        vals = pos.get(slot)
        if vals:
            field = _ES_FIELD_NAME[slot]
            filters.append(_es_term(field, vals[0]) if len(vals) == 1 else _es_terms(field, vals))
    for slot in _SCALAR_MILVUS_SLOTS:
        if slot in exclude_slots:
            continue
        v = pos.get(slot)
        if v and v not in ("n/a", ""):
            filters.append(_es_term(_ES_FIELD_NAME[slot], v))
    # 版型正向：同义词展开为多值 terms（宽松→{宽松,超宽松}）
    if "modeling" not in exclude_slots:
        m = pos.get("modeling")
        if m and m not in ("n/a", ""):
            expanded = expand_modeling(str(m))
            if expanded:
                filters.append(_es_terms("modeling", expanded))
    return filters


def build_role_es_must_not(intent: UserIntent, role: str) -> list[dict[str, Any]]:
    """该 role 的 per-role 否定 → ES bool.must_not。"""
    neg = milvus_filterable_negative(role_negative_slots(intent, role))
    must_not: list[dict[str, Any]] = []
    for slot in _LIST_MILVUS_SLOTS:
        vals = neg.get(slot)
        if not vals:
            continue
        field = _ES_FIELD_NAME[slot]
        must_not.append(_es_term(field, vals[0]) if len(vals) == 1 else _es_terms(field, vals))
    for slot in _SCALAR_MILVUS_SLOTS:
        vals = neg.get(slot)
        if not vals:
            continue
        field = _ES_FIELD_NAME[slot]
        must_not.append(_es_term(field, vals[0]) if len(vals) == 1 else _es_terms(field, vals))
    # 版型否定：多值各自同义词展开后并集 terms
    m_neg = neg.get("modeling")
    if m_neg:
        union: list[str] = []
        seen: set[str] = set()
        for v in m_neg:
            for e in expand_modeling(str(v)):
                if e not in seen:
                    seen.add(e)
                    union.append(e)
        if union:
            must_not.append(_es_terms("modeling", union))
    return must_not


