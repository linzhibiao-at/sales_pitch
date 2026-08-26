"""统一搭配冲突检测引擎：YAML 规则驱动，替代散落的手写 if-else。

用法：
    from backend.ranking.outfit_conflict import check_companion_conflict

    if check_companion_conflict(anchor_row, companion_row):
        continue  # 跳过冲突单品

规则文件：backend/intent/dictionaries/outfit_conflict_rules.yaml
属性提取：backend/intent/sku_attributes.py
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

from backend.intent.sku_attributes import get_attr
from backend.intent.slot_defs import normalize_role
from backend.models import normalize_gender, normalize_genders

logger = logging.getLogger(__name__)

_DICT_DIR = Path(__file__).resolve().parent.parent / "intent" / "dictionaries"
_RULES_FILE = _DICT_DIR / "outfit_conflict_rules.yaml"

# 支持的判定属性。gender 走集合语义（SKU gender 为 list 如 ['男']/'[男,女]'，
# _side_matches 用 normalize_genders 做交集判定，避免 list 的 str repr 失配）。
_ATTR_KEYS: tuple[str, ...] = ("role", "layer", "coverage", "length_class", "is_intimate", "scene_domain", "series", "gender")


@lru_cache(maxsize=1)
def _load_scene_config() -> dict[str, list[str]]:
    """加载 outfit_conflict_rules.yaml 的 scene_allow 配置。

    返回有向允许表 ``{anchor_domain: [允许的 companion 域, ...]}``（归一化、
    去空白、去重，保留原顺序）。有向/非对称：写 ``gym:[tennis]`` 仅表示 gym 锚点
    可推 tennis 候选，反向须另写 ``tennis:[gym]``。``daily`` 不再硬编码，按普通
    键处理。中性 ``""`` 不入表——companion="" 始终放行，anchor=""/未知/未列域
    → 不约束、全量召回。

    本表同时驱动：
      - 下推（build_scene_domain_milvus_expr / build_scene_domain_es_filter）的
        正向允许集（allow 集 ∪ {""}）
      - 成对安全网（_load_rules 据此派生有向 reject 规则注入规则表）
    """
    if not _RULES_FILE.is_file():
        logger.warning("conflict rules file not found: %s", _RULES_FILE)
        return {}
    with _RULES_FILE.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    raw = data.get("scene_allow") or {}
    allow_map: dict[str, list[str]] = {}
    for k, v in raw.items():
        key = str(k).strip()
        if not key:
            continue
        if isinstance(v, (list, tuple, set)):
            vals = [str(x).strip() for x in v if str(x).strip()]
        else:
            vals = [str(v).strip()] if str(v).strip() else []
        # 去重保序
        seen: set[str] = set()
        deduped = [x for x in vals if not (x in seen or seen.add(x))]
        allow_map[key] = deduped
    if not allow_map:
        logger.warning("scene_allow missing/empty in %s", _RULES_FILE.name)
    return allow_map


def _gen_scene_rules(
    allow_map: dict[str, list[str]],
) -> list[dict[str, Any]]:
    """从 scene_allow 有向表派生成对 reject 规则（注入规则表，供
    check_companion_conflict 安全网兜底）。

    有向语义：对每个 anchor 域 A，companion 域 ∉ allow_map[A] 且 ≠ "" → reject。
    每个 A 生成一条规则（companion 侧为该 A 不允许的已知域并集，OR 语义）。
    中性 "" 不进任何 companion 拒绝集 → 始终放行；未知/未列 anchor 不进任何
    anchor 侧 → 无冲突（与 pre-filter 不约束一致）。已知域 = 表键 ∪ 表值并集。
    列表值排序以保证确定性。
    """
    rules: list[dict[str, Any]] = []
    known = sorted(set(allow_map) | {d for v in allow_map.values() for d in v})
    for a in sorted(allow_map):
        allowed = set(allow_map[a]) | {""}
        disallowed = [b for b in known if b not in allowed]
        if disallowed:
            rules.append({
                "name": f"场景冲突 (有向 {a} × 非允许域)",
                "anchor": {"scene_domain": [a]},
                "companion": {"scene_domain": disallowed},
                "action": "reject",
                "_scene_derived": True,
            })
    return rules


def _scene_domain_allow_set(anchor_domain: str) -> list[str]:
    """锚点 scene_domain → 候选侧正向允许的 scene_domain 集合（有向 allow + 中性配件）。

    正向隔离语义（替代原 must_not 排除集）：只放行 scene_allow 表里该锚点域
    显式允许的域 + 中性 ``""`` 配件，其余域一律不召回。中性配件（包/帽/袜等）
    跨场景复用，故 ``""`` 始终并入允许集放行；服装/鞋漏网品已由
    ``extract_scene_domain`` 兜底归 daily，不再落 ``""``。

    有向允许表驱动（见 _load_scene_config）：
      - 锚点域在表中 → ``allow_map[域] ∪ {""}``（如 swim→``["swim", ""]``；
        若写 ``gym:[gym, tennis]`` → gym→``["gym", "tennis", ""]``）
      - 中性 ``""`` / 未知 / 未列域 → ``[]``（锚点无域时不约束候选，全量召回）
    """
    allow_map = _load_scene_config()
    d = (anchor_domain or "").strip()
    if not d or d not in allow_map:
        return []
    return sorted(set(allow_map[d]) | {""})


@lru_cache(maxsize=1)
def _load_series_config() -> dict[str, list[str]]:
    """加载 outfit_conflict_rules.yaml 的 ``series_allow`` 配置（跨系列例外表）。

    返回有向允许表 ``{anchor_series: [额外允许的 companion 系列, ...]}``（归一化、
    去空白、去重、保序）。与 ``scene_allow`` 的语义差异：

      - scene 是**闭合枚举**，未列 anchor 域 → 不约束（全量召回）。
      - series 是**开放枚举**（子品牌线/联名胶囊，值多且动态），默认每系列同系-only
        （自身 + 中性 ``""``），**未列系列亦按 self-only 处理**；此表只写跨系列例外。
      - 中性 ``""`` 不入表——companion="" 始终放行；anchor 无 series → 不约束、全量召回。
    """
    if not _RULES_FILE.is_file():
        logger.warning("conflict rules file not found: %s", _RULES_FILE)
        return {}
    with _RULES_FILE.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    raw = data.get("series_allow") or {}
    allow_map: dict[str, list[str]] = {}
    for k, v in raw.items():
        key = str(k).strip()
        if not key:
            continue
        if isinstance(v, (list, tuple, set)):
            vals = [str(x).strip() for x in v if str(x).strip()]
        else:
            vals = [str(v).strip()] if str(v).strip() else []
        # 去重保序
        seen: set[str] = set()
        deduped = [x for x in vals if not (x in seen or seen.add(x))]
        allow_map[key] = deduped
    return allow_map


def _series_allow_set(anchor_series: str) -> list[str]:
    """锚点 series → 候选侧正向允许的 series 集合（同系-only 默认 + 例外，不含中性）。

    开放枚举语义（与 scene 不同）：

      - 锚点 series 在 ``series_allow`` 例外表中 → ``{锚点} ∪ 例外``
      - 锚点 series 非空但未列表中 → ``{锚点}``（self-only 默认）
      - 锚点无 series / 空串 → ``[]``（不约束候选，全量召回）

    中性 ``""`` **不**并入允许集：anchor 有 series 时，companion series='' 不算匹配
    （空系列不能跨系列复用匹配有系列锚点）。鞋（role=shoes）的豁免在
    ``_series_conflict`` / ``build_series_*`` 另行处理（鞋线 ≠ apparel series）。
    """
    allow_map = _load_series_config()
    s = (anchor_series or "").strip()
    if not s:
        return []
    extras = allow_map.get(s, [])
    return sorted({s} | set(extras))


def _ga_role(sku: Optional[dict[str, Any]]) -> str:
    """取 SKU 的 role（归一化英文，如 shoes/top/bottoms）。局部别名避免循环导入。"""
    from backend.intent.sku_attributes import get_attr as _ga
    if not sku:
        return ""
    return normalize_role(_ga(sku, "role") or "")


def _series_conflict(
    anchor: Optional[dict[str, Any]],
    companion: Optional[dict[str, Any]],
) -> bool:
    """系列冲突安全网：anchor 有 series 且 companion ∉ anchor 允许集 → 冲突。

    开放枚举无法像 ``_gen_scene_rules`` 那样枚举"所有其它域"派生 reject 规则，
    故以专用内联判定兜底（由 ``check_companion_conflict`` 调用）。
    anchor 无 series 时不约束（与下推 ``build_series_*`` 一致）。

    鞋豁免：anchor 或 companion 任一为 shoes → 不约束（鞋线 MILANO 等与 apparel
    series WHITE/ORIGINALE 是不同维度，0 个鞋带 apparel series，强制隔离会召 0 鞋）。
    companion series='' 不再当中性放行（用户要求：空系列不算匹配有系列锚点）。
    """
    if not anchor or not companion:
        return False
    a_series = _ga_series(anchor)
    if not a_series:
        return False  # anchor 无 series → 不约束
    if _ga_role(anchor) == "shoes" or _ga_role(companion) == "shoes":
        return False  # 鞋线 ≠ apparel series，豁免系列隔离
    c_series = _ga_series(companion)
    allow = _series_allow_set(a_series)  # 不含 ""
    return c_series not in allow  # companion='' → not in allow → 冲突


def _ga_series(sku: Optional[dict[str, Any]]) -> str:
    """取 SKU 的 series（原始字段，get_attr 直接返回）。局部别名避免循环导入。"""
    from backend.intent.sku_attributes import get_attr as _ga
    return (_ga(sku, "series") or "").strip()


@lru_cache(maxsize=1)
def _load_rules() -> list[dict[str, Any]]:
    """加载并编译冲突规则。

    规则来源：yaml 显式 rules + 由 scene_allow 派生的有向场景冲突
    reject 规则（见 _gen_scene_rules）。后者保证成对安全网
    ``check_companion_conflict`` 也能拦截非允许域的跨场景冲突。
    """
    if not _RULES_FILE.is_file():
        logger.warning("conflict rules file not found: %s", _RULES_FILE)
        return []
    with _RULES_FILE.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    raw_rules = list(data.get("rules") or [])
    # 注入由 scene_allow 派生的有向场景冲突规则
    allow_map = _load_scene_config()
    raw_rules.extend(_gen_scene_rules(allow_map))

    if not isinstance(raw_rules, list):
        return []
    compiled: list[dict[str, Any]] = []
    for r in raw_rules:
        if not isinstance(r, dict):
            continue
        compiled.append({
            "name": r.get("name", ""),
            "anchor": _compile_side(r.get("anchor") or {}),
            "companion": _compile_side(r.get("companion") or {}),
            "action": r.get("action", "reject"),
        })
    logger.info("loaded %d outfit conflict rules from %s", len(compiled), _RULES_FILE.name)
    return compiled


def _compile_side(side: dict[str, Any]) -> dict[str, frozenset[str]]:
    """将规则一侧的属性条件编译为 frozenset 集合（用于快速交集判定）。"""
    compiled: dict[str, frozenset[str]] = {}
    for key in _ATTR_KEYS:
        val = side.get(key)
        if val is None:
            continue
        if isinstance(val, (list, tuple, set)):
            items = [str(v).strip() for v in val if str(v).strip()]
        else:
            items = [str(val).strip()]
        # role 同时存在中文（上装/下装）与英文（top/bottoms）两种写法，统一归一化
        if key == "role":
            items = [normalize_role(v) for v in items]
        # gender 在 YAML 用规范值（男/女/男童/女童/儿童），归一并集语义：
        # alias（男士→男）也归一，配合 _side_matches 的 normalize_genders 交集判定
        if key == "gender":
            items = [normalize_gender(v) or v for v in items]
        compiled[key] = frozenset(items)
    return compiled


def _side_matches(sku: dict[str, Any], side: dict[str, frozenset[str]]) -> bool:
    """检查 SKU 是否匹配规则一侧的所有属性条件（AND 语义）。"""
    for key, allowed in side.items():
        if key == "role":
            val = normalize_role(get_attr(sku, "role"))
            if val not in allowed:
                return False
        elif key == "gender":
            # SKU gender 是 list（如 ['男'] / ['男','女']），用 normalize_genders 做集合
            # 交集判定：任一规范值命中 allowed 即匹配（双性款命中单性规则 = 放行，不冲突）。
            g_set = normalize_genders(sku.get("gender"))
            if not (g_set & allowed):
                return False
        else:
            val = get_attr(sku, key)
            if val not in allowed:
                return False
    return True


def check_companion_conflict(
    anchor: Optional[dict[str, Any]],
    companion: Optional[dict[str, Any]],
    *,
    bypass_all: bool = False,
) -> bool:
    """检查锚点单品与互补单品是否存在搭配冲突。

    遍历所有规则，任一规则两侧均匹配则返回 True。

    ``bypass_all``：用户对该 companion 所在 target_role 有任一显式 positive
    （见 ``role_slots.role_has_explicit_positive``）时传 True，**一律跳过所有
    锚点驱动的冲突规则**——用户明确意图优先于锚点假设。positive 已在 ES/Milvus
    正向过滤与 ``_item_violates_intent`` 中强制候选符合用户值，故安全网让路不会
    放行不符用户意图的单品，只会避免锚点规则反杀用户要的款（如长袖锚点 ×
    用户要的短裤、daily 锚点 × 唯一的 golf 白色长裤）。
    """
    if not anchor or not companion:
        return False
    # bypass_all：用户对该 companion 所在 target_role 有任一显式 positive → 一律跳过
    # 所有锚点驱动冲突规则（含系列隔离安全网），用户明确意图优先于锚点假设。
    if bypass_all:
        return False
    # 系列冲突安全网（开放枚举，专用内联判定；与下推 build_series_* 对齐）
    if _series_conflict(anchor, companion):
        logger.info(
            "[conflict·系列] anchor=%s companion=%s → reject (系列隔离)",
            anchor.get("sku_id") or anchor.get("title", "")[:20],
            companion.get("sku_id") or companion.get("title", "")[:20],
        )
        return True
    rules = _load_rules()
    for r in rules:
        if _side_matches(anchor, r["anchor"]) and _side_matches(companion, r["companion"]):
            logger.info(
                "[conflict·%s] anchor=%s companion=%s → %s",
                r["name"],
                anchor.get("sku_id") or anchor.get("title", "")[:20],
                companion.get("sku_id") or companion.get("title", "")[:20],
                r["action"],
            )
            return True
    return False


def check_outfit_conflict(
    anchor: Optional[dict[str, Any]],
    items: list[dict[str, Any]],
    anchor_id: str = "",
    *,
    role_bypass_all: Optional[set[str]] = None,
) -> bool:
    """检查整套搭配中是否存在冲突（任一非锚点单品冲突即返回 True）。

    用于固定搭配（anchor_graph）的整套级过滤。

    ``role_bypass_all``：用户有任一显式 positive 的 role 集合，按 item 的 role
    取值下传给 ``check_companion_conflict``，使固定搭配库同样尊重用户显式意图
    （该 role 的所有锚点驱动冲突规则一律让路）。
    """
    if not anchor or not items:
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        if anchor_id and (
            item.get("sku_id") == anchor_id
            or item.get("is_master")
            or item.get("is_anchor")
        ):
            continue
        item_bypass = False
        if role_bypass_all:
            ir = normalize_role(item.get("role") or get_attr(item, "role"))
            if ir and ir in role_bypass_all:
                item_bypass = True
        if check_companion_conflict(anchor, item, bypass_all=item_bypass):
            return True
    return False


def get_excluded_categories_for_milvus(anchor: dict[str, Any]) -> list[str] | None:
    """[deprecated] 为 Milvus 粗排生成 category_l2 排除列表。

    当锚点为长袖上装时，排除短裤/五分/七分裤中类。
    这是 check_companion_conflict 的粗排前置——在 Milvus 查询阶段就过滤掉
    明确冲突的中类，减少 Python 精排的压力。

    已被 ``build_attr_milvus_expr`` 取代：后者直接用 length_class/coverage/layer
    标量字段下推过滤，覆盖更全（含短裙等未列入中类的短款下装），不再枚举
    category_l2。保留此函数以兼容历史调用与回退。
    """
    from backend.intent.sku_attributes import get_attr as _ga

    anchor_role = _ga(anchor, "role")
    anchor_length = _ga(anchor, "length_class")

    if anchor_role == "top" and anchor_length == "long":
        return [
            "梭织短裤", "针织短裤",
            "梭织五分裤", "针织五分裤",
            "梭织七分裤", "针织七分裤",
            "平角裤",
        ]
    return None


def build_attr_milvus_expr(
    anchor: Optional[dict[str, Any]],
    target_role: str,
    *,
    bypass_all: bool = False,
) -> str | None:
    """根据锚点属性 + 目标 role，生成候选侧单点排除的 Milvus expr 片段。

    把 ``check_companion_conflict`` 中可单侧表达的规则下推到 Milvus 查询阶段，
    减少回查 sku 属性后再过滤的开销。成对规则仍由后置 ``check_companion_conflict``
    安全网兜底。

    常驻 ``is_intimate == "false"``：贴身内衣不参与互补推荐（非锚点驱动，但在
    ``bypass_all`` 时一并让路——见下）。其余规则按锚点属性条件触发，作用于候选侧（companion）：

      - 锚点 role=top 且 length_class=long，目标 role=bottoms → ``length_class != "short"``
        （替代 get_excluded_categories_for_milvus 的中类枚举，覆盖短裙等）
      - 锚点 coverage=full → ``coverage != "full"``（全身装互斥）
      - 锚点 role=top 且 layer=base，目标 role=top → ``layer != "base"``（避免两件内搭）
      - 锚点 role=top 且 layer=outer，目标 role=top → ``layer != "outer"``（避免两件外套）

    ``bypass_all``：用户对该 target_role 有任一显式 positive
    （``role_slots.role_has_explicit_positive``）时传 True，**一律放行**——
    ``is_intimate == "false"`` 与全部锚点驱动子句（length/coverage/layer）都让路，
    用户明确意图优先，positive 已强制候选符合用户值，锚点/安全网结构规则再卡只会
    清零用户要的单品。成对安全网（``check_companion_conflict``）同理让路。

    无 anchor 且非 bypass 时仅返回 ``is_intimate == "false"``。
    """
    from backend.intent.category_l2_pairing import merge_milvus_expr
    from backend.intent.sku_attributes import get_attr as _ga

    parts: list[str] = []

    # is_intimate：贴身内衣不参与互补推荐。非 bypass 时下推（含无 anchor 场景），
    # bypass_all（用户对该 role 有显式 positive）时让路——用户明确要的款优先。
    if not bypass_all:
        parts.append('is_intimate == "false"')

    if anchor and not bypass_all:
        a_role = _ga(anchor, "role")
        a_layer = _ga(anchor, "layer")
        a_cov = _ga(anchor, "coverage")
        a_len = _ga(anchor, "length_class")
        tr = (target_role or "").strip().lower()

        if a_role == "top" and a_len == "long" and tr == "bottoms":
            parts.append('length_class != "short"')
        if a_cov == "full":
            parts.append('coverage != "full"')
        if a_role == "top" and tr == "top":
            if a_layer == "base":
                parts.append('layer != "base"')
            elif a_layer == "outer":
                parts.append('layer != "outer"')

    return merge_milvus_expr(*parts)


def build_scene_domain_milvus_expr(
    anchor: Optional[dict[str, Any]],
    target_role: str,
) -> str | None:
    """根据锚点 scene_domain 生成候选侧正向隔离的 Milvus expr 片段。

    scene_domain 冲突可单侧表达（给定锚点域即可算出 companion 允许域），
    有向允许表驱动（``_scene_domain_allow_set``，配置见 yaml ``scene_allow``）：
      - 锚点域在表中 → ``scene_domain in [allow_map[域], ""]``
        （self-only 默认如 swim→``scene_domain in ["swim", ""]``；可写
        ``gym:[gym, tennis]`` 让 gym 锚点跨项目放行 tennis）
      - 锚点中性 ``""`` / 未知 / 未列域 → None（不加约束，全量召回）

    正向隔离（allow 集 + 中性配件）替代了原 must_not（排除异营）语义：
    中性配件 ``""`` 始终放行以跨场景复用；服装/鞋漏网品由 extract_scene_domain
    兜底归 daily，不再落 ``""``。

    ``target_role`` 当前不参与判定（scene_domain 按 SKU 整体域隔离），
    保留参数以与 ``build_attr_milvus_expr`` 接口对齐。
    """
    from backend.intent.sku_attributes import get_attr as _ga

    if not anchor:
        return None
    a_domain = _ga(anchor, "scene_domain")
    allow = _scene_domain_allow_set(a_domain)
    if not allow:
        return None
    if len(allow) == 1:
        return f'scene_domain == "{allow[0]}"'
    quoted = ",".join(f'"{d}"' for d in allow)
    return f'scene_domain in [{quoted}]'


def build_attr_es_filter(
    anchor: Optional[dict[str, Any]],
    target_role: str,
    *,
    bypass_all: bool = False,
) -> dict[str, Any] | None:
    """根据锚点属性 + 目标 role，生成候选侧单点排除的 ES ``must_not`` 子句。

    镜像 ``build_attr_milvus_expr``，把可单侧表达的结构化规则下推到 ES query：
    减少 ES 命中后再 post-filter 的开销。成对规则仍由 ``check_companion_conflict``
    安全网兜底。

    返回 ``{"must_not": [...]}`` 形式的 ES query DSL 片段，或 None。
    调用方将其并入 bool query 的 must_not（与现有 cat2_filter/cs_filter 串联）。

    常驻 ``is_intimate != true``：贴身内衣不参与互补推荐（非锚点驱动，但在
    ``bypass_all`` 时一并让路——见下）。其余规则按锚点属性条件触发，作用于候选侧（companion）：

      - 锚点 role=top 且 length_class=long，目标 role=bottoms → 排除 ``length_class=short``
      - 锚点 coverage=full → 排除 ``coverage=full``（全身装互斥，单侧部分；
        成对"双全身装"互斥仍由 post-filter 兜底）
      - 锚点 role=top 且 layer=base，目标 role=top → 排除 ``layer=base``
      - 锚点 role=top 且 layer=outer，目标 role=top → 排除 ``layer=outer``

    ``bypass_all``：用户对该 target_role 有任一显式 positive 时传 True，**一律放行**——
    ``is_intimate != true`` 与全部锚点驱动子句（length/coverage/layer）都让路（镜像 Milvus 路）。

    scene_domain 不在此处——见 ``build_scene_domain_es_filter``（正向隔离，
    走 bool.filter 而非 must_not）。
    """
    from backend.intent.sku_attributes import get_attr as _ga

    must_not: list[dict[str, Any]] = []

    # is_intimate：排除贴身内衣。非 bypass 时下推（含无 anchor 场景），
    # bypass_all（用户对该 role 有显式 positive）时让路——用户明确要的款优先。
    if not bypass_all:
        must_not.append({"term": {"is_intimate": True}})

    if anchor and not bypass_all:
        a_role = _ga(anchor, "role")
        a_layer = _ga(anchor, "layer")
        a_cov = _ga(anchor, "coverage")
        a_len = _ga(anchor, "length_class")
        tr = (target_role or "").strip().lower()

        if a_role == "top" and a_len == "long" and tr == "bottoms":
            must_not.append({"term": {"length_class": "short"}})
        if a_cov == "full":
            must_not.append({"term": {"coverage": "full"}})
        if a_role == "top" and tr == "top":
            if a_layer == "base":
                must_not.append({"term": {"layer": "base"}})
            elif a_layer == "outer":
                must_not.append({"term": {"layer": "outer"}})

    if not must_not:
        return None
    return {"must_not": must_not}


def build_scene_domain_es_filter(
    anchor: Optional[dict[str, Any]],
    target_role: str,
) -> dict[str, Any] | None:
    """根据锚点 scene_domain 生成候选侧正向隔离的 ES filter 子句。

    镜像 ``build_scene_domain_milvus_expr``，把 scene_domain 正向隔离下推到 ES
    query 的 bool.filter（而非 must_not）：只放行 allow 集 + 中性，减少 ES 命中后再
    post-filter 的开销。成对非允许域规则仍由 ``check_companion_conflict``
    安全网兜底。

    返回 ``{"terms": {"scene_domain": [allow...]}}`` 形式的 ES filter 片段，或 None。
    调用方将其并入 bool query 的 filter（与 cat2_filter/cs_filter 串联）。

      - 锚点域在表中 → 仅召回 ``allow_map[域]`` + 中性配件 ``""``
        （self-only 默认如 swim→仅召回 ``swim`` 与中性配件；可写
        ``gym:[gym, tennis]`` 让 gym 锚点跨项目召回 tennis）
      - 锚点中性 ``""`` / 未知 / 未列域 → None（不加约束，全量召回）

    中性配件 ``""`` 始终放行以跨场景复用；服装/鞋漏网品由 extract_scene_domain
    兜底归 daily，不再落 ``""``。

    ``target_role`` 当前不参与判定，保留以与 ``build_attr_es_filter`` 接口对齐。
    """
    from backend.intent.sku_attributes import get_attr as _ga

    if not anchor:
        return None
    a_domain = _ga(anchor, "scene_domain")
    allow = _scene_domain_allow_set(a_domain)
    if not allow:
        return None
    return {"terms": {"scene_domain": allow}}


def build_series_milvus_expr(
    anchor: Optional[dict[str, Any]],
    target_role: str,
    intent_series: str = "",
    *,
    bypass_all: bool = False,
) -> str | None:
    """根据锚点 series 生成候选侧正向隔离的 Milvus expr 片段（镜像 scene_domain）。

    series 同系-only 默认 + 例外（``_series_allow_set``，配置见 yaml ``series_allow``）：
      - 锚点有 series → ``series in [允许集]``（允许集 = 自身 ∪ 例外 ∪ {""}，
        self-only 默认如 GOLF→``series in ["GOLF", ""]``；可写例外跨系列放行）
      - 锚点无 series 但 ``intent_series`` 非空（text_only 用户显式提系列）→ 以
        intent_series 为锚点系列同样下推，让纯文本系列请求也能同系列召回
      - 两者均空 → None（不加约束，全量召回）

    锚点 SKU 的 series 权威：anchor 有 series 时忽略 intent_series（避免与锚点数据冲突）。
    鞋豁免：target_role=shoes 或 anchor role=shoes → None（不加约束）。鞋线 ≠ apparel
    series（0 个鞋带 apparel series，强制隔离会召 0 鞋）。
    ``target_role`` 当前仅在鞋豁免判定中参与，其余不参与判定。

    ``bypass_all``：用户对该 target_role 有任一显式 positive
    （``role_slots.role_has_explicit_positive``）时传 True，**一律放行**（返回 None）——
    与 scene_domain/length/coverage/layer 对称，用户明确意图优先于锚点同系假设。
    成对安全网 ``_series_conflict`` 同理让路（``check_companion_conflict`` bypass_all 早返回）。
    """
    if bypass_all:
        return None
    if normalize_role((target_role or "").strip()) == "shoes":
        return None
    if anchor and _ga_role(anchor) == "shoes":
        return None
    if not anchor:
        a_series = ""
    else:
        a_series = _ga_series(anchor)
    effective = a_series or (intent_series or "").strip()
    allow = _series_allow_set(effective)
    if not allow:
        return None
    if len(allow) == 1:
        return f'series == "{allow[0]}"'
    quoted = ",".join(f'"{s}"' for s in allow)
    return f'series in [{quoted}]'


def build_series_es_filter(
    anchor: Optional[dict[str, Any]],
    target_role: str,
    intent_series: str = "",
    *,
    bypass_all: bool = False,
) -> dict[str, Any] | None:
    """根据锚点 series 生成候选侧正向隔离的 ES filter 子句（镜像 Milvus expr）。

    返回 ``{"terms": {"series": [allow...]}}`` 形式的 ES filter 片段，或 None。
    调用方将其并入 bool query 的 filter（与 scene_filter 等串联）。
    ``intent_series`` 语义同 ``build_series_milvus_expr``（text_only 回退锚点系列）。
    鞋豁免同 Milvus 路：target_role=shoes 或 anchor role=shoes → None。

    ``bypass_all``：用户对该 target_role 有任一显式 positive 时传 True，一律放行（返回 None），
    与 scene_domain 对称（见 ``build_series_milvus_expr``）。
    """
    if bypass_all:
        return None
    if normalize_role((target_role or "").strip()) == "shoes":
        return None
    if anchor and _ga_role(anchor) == "shoes":
        return None
    if not anchor:
        a_series = ""
    else:
        a_series = _ga_series(anchor)
    effective = a_series or (intent_series or "").strip()
    allow = _series_allow_set(effective)
    if not allow:
        return None
    return {"terms": {"series": allow}}
