"""API Pydantic 模型（营销话术）。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

# lone surrogate(U+D800~U+DFFF)在 Python3 str 中是非法码点, 无法 utf-8 编码,
# 下游(LLM 请求体 / ES)会抛 UnicodeEncodeError → 500 泄露内部异常类型。
# 合法补充字符(emoji 等)是单码点, 不在此范围, 不受影响。
import re as _re

_SURROGATE_RE = _re.compile(r"[\ud800-\udfff]")

# <script> 等 HTML 标签会触发 LLM 内容过滤导致无输出。
# 剥除所有 HTML 标签（含 <script>...</script>、<img>、<iframe> 等）。
_HTML_TAG_RE = _re.compile(r"<[^>]+>")
_SCRIPT_BLOCK_RE = _re.compile(
    r"<script[^>]*>.*?</script>", _re.IGNORECASE | _re.DOTALL
)

# extra_prompt（导购自由补充要求）长度上限（字符数，按清理后长度校验）
_EXTRA_PROMPT_MAX_LEN = 500

# coupon_names 单项券名长度上限（字符数，按清理后长度校验）
_COUPON_NAME_MAX_LEN = 100


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


# ── 对外营销话术接口（/v1/sales-pitch/generate）──
class SalesPitchCustomerInfo(BaseModel):
    """顾客画像：union_id 必传（用于 session_id 生成），其余按需传入。"""

    # 会员标识（必传）：微信 unionid 等平台标识，用于确定性生成 session_id
    union_id: str = Field(min_length=1)
    # 顾客称呼（如"王女士""李先生"），话术可直接用来拉近距离
    nickname: Optional[str] = None
    gender: Optional[str] = None
    # 年龄段或具体年龄（如"35""大学生""中大童"）
    age: Optional[str] = None
    # 会员等级（如"金卡会员"），供权益类话术使用
    member_level: Optional[str] = None
    # 会员积分（非负），供权益类话术使用
    points: Optional[int] = None
    # 扩展字段：以"字段名→值"形式原样注入 prompt
    extra: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("union_id", mode="after")
    @classmethod
    def _strip_union_id(cls, v: str) -> str:
        return v.strip()

    @field_validator("nickname", "age", "member_level", mode="after")
    @classmethod
    def _sanitize_text_fields(cls, v: object) -> object:
        # 自由文本：剥 HTML 标签(防 LLM 内容过滤) + lone surrogate
        return _strip_html_tags(_strip_surrogates(v))

    @field_validator("points", mode="after")
    @classmethod
    def _check_points(cls, v: object) -> object:
        if isinstance(v, int) and v < 0:
            raise ValueError("points must be >= 0")
        return v

    @field_validator("*", mode="after")
    @classmethod
    def _sanitize_str_fields(cls, v: object) -> object:
        return _strip_surrogates(v)


class SalesPitchProductInfo(BaseModel):
    """商品信息：商品名称 + 颜色 + extra 自由扩展。"""

    # 商品名称（必填）：话术中直接称呼的商品名；非空校验在清理 HTML/surrogate
    # 之后做（带 min_length 约束的 str 会在清理前拒绝 surrogate，与剥除降级
    # 策略不一致，故用裸 str + after validator）
    title: str
    color: Optional[str] = None
    extra: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("title", mode="after")
    @classmethod
    def _sanitize_title(cls, v: object) -> object:
        # 自由文本：剥 HTML 标签 + lone surrogate，清理后须非空
        cleaned = _strip_html_tags(_strip_surrogates(v))
        if isinstance(cleaned, str) and not cleaned.strip():
            raise ValueError("title must not be empty")
        return cleaned

    @field_validator("*", mode="after")
    @classmethod
    def _sanitize_str_fields(cls, v: object) -> object:
        return _strip_surrogates(v)


class SalesPitchPromotionInfo(BaseModel):
    """门店已选活动：promo_id 供审计溯源（不注入提示词）+ copy 展示文案（生成引用原文）。"""

    promo_id: str
    # 活动名（可选，展示用）
    name: Optional[str] = None
    # 活动展示文案（必填）：接口字段名为 copy（与 BaseModel.copy 方法冲突，
    # 内部属性名用 copy_text + alias 保持接口不变；序列化取 by_alias=True）
    # 话术仅可引用该原文，禁止编造；非空校验在清洗后做
    copy_text: str = Field(alias="copy")

    @field_validator("promo_id", mode="after")
    @classmethod
    def _strip_promo_id(cls, v: object) -> object:
        if isinstance(v, str):
            cleaned = v.strip()
            if not cleaned:
                raise ValueError("promo_id must not be empty")
            return cleaned
        return v

    @field_validator("copy_text", mode="after")
    @classmethod
    def _sanitize_copy(cls, v: object) -> object:
        # 自由文本：剥 HTML 标签 + lone surrogate 后 strip，清理后须非空
        cleaned = _strip_html_tags(_strip_surrogates(v))
        if isinstance(cleaned, str):
            cleaned = cleaned.strip()
            if not cleaned:
                raise ValueError("copy must not be empty")
        return cleaned

    @field_validator("name", mode="after")
    @classmethod
    def _sanitize_text_fields(cls, v: object) -> object:
        # 自由文本：剥 HTML 标签(防 LLM 内容过滤) + lone surrogate
        return _strip_html_tags(_strip_surrogates(v))

    @field_validator("*", mode="after")
    @classmethod
    def _sanitize_str_fields(cls, v: object) -> object:
        return _strip_surrogates(v)


class SalesPitchRequest(BaseModel):
    """营销话术生成入参：顾客信息 + 商品信息 → LLM 生成导购话术。"""

    app_id: str
    # 导购工号（导购身份标识; 开关与 mock 用户列表见 guide_auth 配置段）
    guide_num: str = Field(min_length=1)
    # 顾客信息必传：union_id 用于 session_id 生成，其余字段按需传入
    customer: SalesPitchCustomerInfo
    # 至少 1 个商品；上限防御由路由层校验
    products: List[SalesPitchProductInfo] = Field(min_length=1, max_length=10)
    # 门店已选 POS 活动（可选）：非空时注入【促销活动】块，文案仅可引用 copy 原文
    promotions: Optional[List[SalesPitchPromotionInfo]] = Field(default=None, max_length=10)
    # 已核验且已选的顾客券名（可选）：非空注入券名；显式空列表触发"会员专属
    # 优惠"兜底指示；缺省不注入（旧调用方零差异）
    coupon_names: Optional[List[str]] = Field(default=None, max_length=20)
    # 话术风格：warm(热情亲切)/professional(专业顾问)/concise(简短干练)或自由描述
    pitch_style: Optional[str] = None
    # 话术字数上限（0 或缺失表示不限）
    max_length: Optional[int] = None
    # 导购自由补充要求（可选）：非空时作为【补充要求】块注入提示词
    extra_prompt: Optional[str] = None

    @field_validator("pitch_style", "guide_num", mode="after")
    @classmethod
    def _strip_text(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v

    @field_validator("max_length", mode="after")
    @classmethod
    def _check_max_length(cls, v: object) -> object:
        if isinstance(v, int) and v < 0:
            raise ValueError("max_length must be >= 0")
        return v

    @field_validator("extra_prompt", mode="after")
    @classmethod
    def _sanitize_extra_prompt(cls, v: object) -> object:
        # 自由文本：剥 HTML 标签(防 LLM 内容过滤) + lone surrogate 后 strip；
        # 按清理后长度校验上限（带约束的 Field 会在清理前拒绝，与剥除降级策略不一致）
        if not isinstance(v, str):
            return v
        cleaned = _strip_html_tags(_strip_surrogates(v)).strip()
        if len(cleaned) > _EXTRA_PROMPT_MAX_LEN:
            raise ValueError(
                f"extra_prompt must be at most {_EXTRA_PROMPT_MAX_LEN} characters"
            )
        return cleaned

    @field_validator("coupon_names", mode="after")
    @classmethod
    def _sanitize_coupon_names(cls, v: object) -> object:
        # 逐项清洗（HTML + lone surrogate + strip）并剔除空白项；保持空列表形态
        # （显式 [] 与缺省 None 语义不同：前者触发"会员专属优惠"兜底）。
        # 单项超长在清理后校验（带 max_length 约束的 str 会在清理前拒绝）。
        if not isinstance(v, list):
            return v
        cleaned: list[str] = []
        for item in v:
            if not isinstance(item, str):
                raise ValueError("coupon_names items must be strings")
            name = _strip_html_tags(_strip_surrogates(item)).strip()
            if not name:
                continue
            if len(name) > _COUPON_NAME_MAX_LEN:
                raise ValueError(
                    f"coupon_names items must be at most "
                    f"{_COUPON_NAME_MAX_LEN} characters"
                )
            cleaned.append(name)
        return cleaned

    @field_validator("*", mode="after")
    @classmethod
    def _sanitize_str_fields(cls, v: object) -> object:
        return _strip_surrogates(v)
