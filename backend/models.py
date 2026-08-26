"""API 与领域 Pydantic 模型。"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, StrictBool, field_validator

# lone surrogate(U+D800~U+DFFF)在 Python3 str 中是非法码点, 无法 utf-8 编码,
# 下游(jsonl replay 落盘 / LLM 请求体 / ES)会抛 UnicodeEncodeError → 500 泄露
# 内部异常类型(ISS-06)。合法补充字符(emoji 等)是单码点, 不在此范围, 不受影响。
import re as _re

_SURROGATE_RE = _re.compile(r"[\ud800-\udfff]")

# ISS-09: <script> 等 HTML 标签会触发 LLM 内容过滤导致无推荐结果。
# 剥除所有 HTML 标签（含 <script>...</script>、<img>、<iframe> 等）。
_HTML_TAG_RE = _re.compile(r"<[^>]+>")
_SCRIPT_BLOCK_RE = _re.compile(
    r"<script[^>]*>.*?</script>", _re.IGNORECASE | _re.DOTALL
)


def _strip_surrogates(v: object) -> object:
    """剔除字符串中的 lone surrogate, 使其可安全 utf-8 编码。非 str 原样返回。"""
    if isinstance(v, str) and _SURROGATE_RE.search(v):
        return _SURROGATE_RE.sub("", v)
    return v


def _strip_html_tags(v: object) -> object:
    """剔除字符串中的 HTML 标签（含 <script> 块），防 LLM 内容过滤。非 str 原样返回。"""
    if not isinstance(v, str):
        return v
    s = _SCRIPT_BLOCK_RE.sub("", v)
    s = _HTML_TAG_RE.sub("", s)
    return s


# 锚点 SKU 货号格式(ISS-02): 仅拒"格式垃圾", 不强求完整 FILA 语法。
# 真实 SKU 全部 ^[A-Z][0-9] 开头、大写字母+数字(个别含连字符 S2128116-1),
# 长度 9~18。截断前缀(如 A11M627701)与合法短码(如 U2D240211)同构, 无法纯
# 正则区分, 故查不到仍走 200+空降级; 仅对 小写/中文/符号/纯数字/SQL/XSS 等
# 明显非法格式返 400 invalid sku_id format。
_SKU_ID_RE = _re.compile(r"^[A-Z][0-9][A-Z0-9-]*$")


def is_valid_sku_id_format(s: str) -> bool:
    """input_sku_id 是否符合基本格式(非空且匹配)。不保证 SKU 存在。"""
    return bool(s) and bool(_SKU_ID_RE.match(s))

QueryType = Literal[
    "item_to_outfit",
    "outfit_reference",
    "text_only",
]

# ── gender 归一化 ──────────────────────────────────────────────
GENDER_CANONICAL = frozenset({"男", "女", "男童", "女童", "儿童"})

_GENDER_ALIAS: dict[str, str] = {
    "男": "男", "男士": "男", "男生": "男", "男性": "男", "男装": "男",
    "爸爸": "男", "老公": "男",
    "女": "女", "女士": "女", "女生": "女", "女性": "女", "女装": "女",
    "妈妈": "女", "老婆": "女",
    "男童": "男童", "男孩": "男童", "男宝": "男童", "小男生": "男童",
    "儿子": "男童", "小男孩": "男童",
    "女童": "女童", "女孩": "女童", "女宝": "女童", "小女生": "女童",
    "女儿": "女童", "小女孩": "女童",
    "儿童": "儿童", "童装": "儿童", "小朋友": "儿童", "孩子": "儿童",
    "宝宝": "儿童", "小孩": "儿童",
}


def normalize_gender(value: object) -> Optional[str]:
    """将各种性别表述归一化为 男/女/男童/女童/儿童，无法识别时返回 None。"""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s in GENDER_CANONICAL:
        return s
    return _GENDER_ALIAS.get(s)


_GENDER_MULTI_ALIAS: dict[str, list[str]] = {
    "男女同款": ["男", "女"],
    "男女": ["男", "女"],
    "男/女": ["男", "女"],
    "男女随机": ["男", "女"],
    "男女童": ["男童", "女童"],
    "中性": ["男", "女"],
    "中": ["男", "女"],
    "中性款": ["男", "女"],
    "通用": ["男", "女"],
    "不分性别": ["男", "女"],
    "UNISEX": ["男", "女"],
    "MAN": ["男"],
    "WOMAN": ["女"],
}


def normalize_genders(value: object) -> set[str]:
    """将 gender（字符串或列表）归一化为标准值集合。

    元素来自 {男,女,男童,女童,儿童}；多值/中性表述展开为多元素。
    用于 ranking 阶段对 ETL 产出的 list 化 gender 做集合交集判断。
    """
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    out: set[str] = set()
    for v in items:
        s = str(v).strip() if v is not None else ""
        if not s:
            continue
        if s in GENDER_CANONICAL:
            out.add(s)
            continue
        alias = _GENDER_ALIAS.get(s)
        if alias:
            out.add(alias)
            continue
        multi = _GENDER_MULTI_ALIAS.get(s)
        if multi:
            out.update(multi)
    return out


def normalize_gender_first(value: object) -> Optional[str]:
    """从 gender（字符串或列表）取首个归一化值，用于需要单值的场景。"""
    gs = normalize_genders(value)
    if not gs:
        return None
    for v in ("男", "女", "男童", "女童", "儿童"):
        if v in gs:
            return v
    return next(iter(gs))


# ── age 归一化（童装年龄段，与 gender 正交的独立维度） ─────────
# 源数据取值：小童 / 中大童 / 婴幼童 / 通码；空值=成人款或未分段。
# 通码 = 同款覆盖小童~中大童，查询任一童装段时均应命中。
AGE_CANONICAL = frozenset({"小童", "中大童", "婴幼童", "通码"})

_AGE_ALIAS: dict[str, str] = {
    "小童": "小童",
    "中大童": "中大童",
    "大童": "中大童",
    "中童": "中大童",
    "婴幼童": "婴幼童",
    "婴儿": "婴幼童",
    "婴幼": "婴幼童",
    "幼童": "婴幼童",
    "通码": "通码",
}


def normalize_age(value: object) -> Optional[str]:
    """将童装年龄段表述归一化为 小童/中大童/婴幼童/通码，无法识别时返回 None。

    噪声值（如源端误填的 "33"）返回 None。直接保留源端取值，不重分桶。
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s in AGE_CANONICAL:
        return s
    return _AGE_ALIAS.get(s)


# ── season 归一化 ─────────────────────────────────────────────
import re as _re

SEASON_CANONICAL = frozenset({"春", "夏", "秋", "冬"})

_SEASON_ALIAS: dict[str, str] = {
    "春": "春", "春季": "春", "春天": "春",
    "夏": "夏", "夏季": "夏", "夏天": "夏", "夏日": "夏",
    "秋": "秋", "秋季": "秋", "秋天": "秋",
    "冬": "冬", "冬季": "冬", "冬天": "冬", "寒冬": "冬",
}

# 多季节别名 → 映射到多个标准季节
_SEASON_MULTI_ALIAS: dict[str, list[str]] = {
    "春夏": ["春", "夏"],
    "秋冬": ["秋", "冬"],
    "常青": ["春", "夏", "秋", "冬"],
    "四季": ["春", "夏", "秋", "冬"],
}

# Q 季度 → 标准季节
_Q_TO_SEASON: dict[str, str | list[str]] = {
    "1": "春", "Q1": "春",
    "2": "夏", "Q2": "夏",
    "3": "秋", "Q3": "秋",
    "4": "冬", "Q4": "冬",
}
# Q5/Q6 = 常青/四季
_Q_ALL_SEASONS: set[str] = {"5", "6", "Q5", "Q6"}

# FW / SS 等英文季节缩写
_EN_SEASON_ALIAS: dict[str, list[str]] = {
    "FW": ["秋", "冬"],
    "AW": ["秋", "冬"],
    "SS": ["春", "夏"],
}

# 匹配 Q 季度格式: 可选年份前缀 + Q + 数字, 如 "Q4", "24Q4", "2025Q3"
_RE_Q_SEASON = _re.compile(r"(?:\d{2,4})?Q([1-6])", _re.IGNORECASE)
# 匹配 FW/SS + 可选年份, 如 "FW22", "SS21"
_RE_EN_SEASON = _re.compile(r"(FW|AW|SS)\d{0,4}", _re.IGNORECASE)


def _add_season(canon: str, out: list[str], seen: set[str]) -> None:
    if canon and canon not in seen:
        seen.add(canon)
        out.append(canon)


def _extract_seasons_from_token(s: str, out: list[str], seen: set[str]) -> bool:
    """尝试从单个 token 中提取季节，返回是否成功。"""
    # 1) 直接别名匹配
    canon = _SEASON_ALIAS.get(s)
    if canon:
        _add_season(canon, out, seen)
        return True

    # 2) 多季节别名 (春夏、秋冬、常青)
    multi = _SEASON_MULTI_ALIAS.get(s)
    if multi:
        for c in multi:
            _add_season(c, out, seen)
        return True

    # 3) Q 季度格式: "Q4", "24Q4", "2025Q3", "Q5"/"Q6"(常青/四季)
    m = _RE_Q_SEASON.search(s)
    if m:
        q_num = m.group(1)
        q_key = f"Q{q_num}"
        if q_key in _Q_ALL_SEASONS:
            for c in ("春", "夏", "秋", "冬"):
                _add_season(c, out, seen)
            return True
        canon = _Q_TO_SEASON.get(q_key)
        if canon:
            _add_season(canon, out, seen)
            return True
        return False

    # 4) FW/SS 英文季节缩写
    m = _RE_EN_SEASON.match(s)
    if m:
        key = m.group(1).upper()
        for c in _EN_SEASON_ALIAS.get(key, []):
            _add_season(c, out, seen)
        return True

    # 5) 首字符是标准季节字
    if len(s) >= 1 and s[0] in SEASON_CANONICAL:
        _add_season(s[0], out, seen)
        return True

    return False


def normalize_season(raw: object) -> list[str]:
    """将各种季节表述归一化为 春/夏/秋/冬 列表，去重保序。

    支持格式：
    - 中文: 春/夏/秋/冬/春季/夏季/秋冬/春夏/常青 等
    - Q 季度: Q1~Q4, 24Q4, 2025Q3 等
    - 英文缩写: FW22, SS21, AW20 等
    - 混合: "Q4 / 24Q4", "秋季 / 2025Q3" (按 "/" 拆分)
    """
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        s = str(item).strip()
        if not s:
            continue
        # 按 "/" 拆分混合格式，如 "Q4 / 24Q4" 或 "秋季 / 2025Q3"
        parts = [p.strip() for p in s.split("/") if p.strip()]
        for part in parts:
            _extract_seasons_from_token(part, out, seen)
    return out


# 跨季兼容：春夏/秋冬两季划分（零售业标准 SS/AW 分组）。
# 春↔夏、秋↔冬 互相兼容；春锚点放行夏款、冬装外套仍挡夏款（保护跨季初衷）。
# 常青/四季 在上游 normalize_season 已展开为四季，经本表展开后仍为全集，不丢季。
_SEASON_COMPAT_PAIRS: dict[str, str] = {
    "春": "夏",
    "夏": "春",
    "秋": "冬",
    "冬": "秋",
}


def season_compatible_set(seasons: list[str]) -> list[str]:
    """把 want 季节集按跨季兼容矩阵展开为「应放行」的季节列表，去重保序。

    用于 season 粗排（Milvus ``season like`` / ES ``wildcard``）与 ``season_conflict``
    安全网：春锚点应同时放行夏款，避免长袖春装锚点把所有夏/秋款下装清零
    （即便用户显式要的某系列下装库里只有夏/秋款，也能召回来）。

    输入应为 normalize_season 产出的单季 token（春/夏/秋/冬）；未知/空 token 原样保留
    不展开（不丢，交由调用方/DB 比对）。
    """
    out: list[str] = []
    seen: set[str] = set()
    for s in seasons or []:
        token = str(s).strip()
        if not token or token in seen:
            continue
        out.append(token)
        seen.add(token)
        compat = _SEASON_COMPAT_PAIRS.get(token)
        if compat and compat not in seen:
            out.append(compat)
            seen.add(compat)
    return out


class UserIntent(BaseModel):
    query_type: QueryType = "text_only"
    text: str = ""
    image_base64: Optional[str] = None
    anchor_role: Optional[str] = None
    target_roles: List[str] = Field(default_factory=list)
    gender: Optional[str] = None
    age: Optional[str] = None
    season: List[str] = Field(default_factory=list)
    occasion_tags: List[str] = Field(default_factory=list)
    style_tags: List[str] = Field(default_factory=list)
    color: List[str] = Field(default_factory=list)
    color_series: List[str] = Field(default_factory=list)
    category: List[str] = Field(default_factory=list)
    length_class: Optional[str] = None
    coverage: Optional[str] = None
    scene_domain: Optional[str] = None
    # 子品牌线/联名胶囊（ORIGINALE/GOLF/HERITAGE/FILA FUSION LIFE/FILA X NEMEN…）；
    # 开放枚举，normalize_series 校验。锚点有 series 时由 SKU 数据权威驱动；
    # text_only 用户显式提系列时由意图提取，下推为 series 隔离的回退锚点系列。
    series: Optional[str] = None
    # 版型（产品款型）：枚举 宽松/基础/舒适/修身/紧身/超宽松/ACTIVE；同义词归并见 sku_attributes.expand_modeling
    modeling: Optional[str] = None
    budget_max: Optional[float] = None
    # 价格下限（与 budget_max 共同表达区间）；per-role 覆盖见 target_slots[role].positive.budget_min
    budget_min: Optional[float] = None
    # per-target-role 槽位（key=role token 或 "*" 全局）：
    #   {"positive": {slot: scalar|list}, "negative": {slot: [values]}}
    # positive=该 role 正向覆盖（未出现则沿用顶层 flat）；negative=该 role 否定。
    # "*" 仅承载 negative（全局否定），positive 恒空。详见 intent_extract.md 第十七节。
    target_slots: Dict[str, Dict[str, Dict[str, Any]]] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str = ""
    image_base64: Optional[str] = None
    selected_sku_id: Optional[str] = None
    selected_spu_id: Optional[str] = None
    ranking_scoring_method: Optional[str] = None
    skip_reason: Optional[bool] = None
    enable_llm_rank_reason: Optional[bool] = None
    llm_model: Optional[str] = None
    enable_tryon: Optional[bool] = None
    tryon_person_image: Optional[str] = None


class RecommendSkusRequest(BaseModel):
    anchor_sku_id: Optional[str] = None
    anchor_spu_id: Optional[str] = None
    target_roles: List[str] = Field(
        default_factory=lambda: ["bottoms", "shoes"],
    )
    filters: Dict[str, Any] = Field(default_factory=dict)
    limit_per_role: int = 6


class RecommendOutfitsRequest(BaseModel):
    query: str = ""
    image_base64: Optional[str] = None
    filters: Dict[str, Any] = Field(default_factory=dict)
    limit: int = 6


class RegenerateReasonRequest(BaseModel):
    outfit_id: str
    message: Optional[str] = None
    llm_model: Optional[str] = None


# ── 对外接口（按 docs/FILA穿搭推荐入参出参.md）──
class ExternalRecommendRequest(BaseModel):
    session_id: Optional[str] = None
    app_id: str
    # 非必填字段：Pydantic v2 中显式传 null 需类型含 None 才不报 422
    # (默认值仅在键缺失时生效)。下游一律以 `req.message or ""` 消费,对 None 安全。
    message: Optional[str] = ""
    image_url: Optional[str] = None
    # 锚点 SKU 货号；与 image_url/message 至少传一个
    input_sku_id: Optional[str] = None
    # 严格布尔：拒绝 1/0/"true"/"false" 等隐式转换，避免误触发试穿（22s+）
    # null 视同 false（未传试穿意图）
    tryon: Optional[StrictBool] = False
    # 话术风格：透传，暂不接入理由生成
    reason_style: Optional[str] = None

    @field_validator("message", mode="after")
    @classmethod
    def _sanitize_message(cls, v: object) -> object:
        """ISS-09: 剔除 message 中的 HTML/script 标签，防 LLM 内容过滤返空。"""
        return _strip_html_tags(_strip_surrogates(v))

    @field_validator("input_sku_id", mode="after")
    @classmethod
    def _strip_input_sku_id(cls, v: object) -> object:
        """ISS-03: 模型层 strip 前后空格，确保响应回显与 ES 查询统一使用干净值。"""
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("*", mode="after")
    @classmethod
    def _sanitize_str_fields(cls, v: object) -> object:
        # 入参边界剔除 lone surrogate, 防止下游 utf-8 编码崩溃(ISS-06)
        return _strip_surrogates(v)


class ExternalRegenerateReasonRequest(BaseModel):
    # min_length=1: 空串按必填缺失处理 → 422, 而非落到 service 查找返 404
    outfit_id: str = Field(min_length=1)
    reason_style: Optional[str] = None

    @field_validator("*", mode="after")
    @classmethod
    def _sanitize_str_fields(cls, v: object) -> object:
        return _strip_surrogates(v)


class ImageUnderstandingResult(BaseModel):
    image_kind: str = "unknown"
    anchor_role: Optional[str] = None
    colors: List[str] = Field(default_factory=list)
    style_tags: List[str] = Field(default_factory=list)
    confidence: float = 0.0
    raw: Dict[str, Any] = Field(default_factory=dict)
