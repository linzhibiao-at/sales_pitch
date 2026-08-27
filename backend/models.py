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
    """顾客画像（全部可选，按需传入；未提供的维度不注入 prompt）。"""

    # 顾客称呼（如"王女士""李先生"），话术可直接用来拉近距离
    nickname: Optional[str] = None
    gender: Optional[str] = None
    # 年龄段或具体年龄（如"35""大学生""中大童"）
    age: Optional[str] = None
    # 风格偏好（如"简约通勤""复古运动"）
    style_preference: Optional[str] = None
    # 使用/穿着场景（如"秋季通勤""周末出游""开学季"）
    scene: Optional[str] = None
    # 尺码/身材信息（如"M 码""173cm/60kg"）
    size_info: Optional[str] = None
    # 预算范围（如"500-800元"）
    budget: Optional[str] = None
    # 导购补录的顾客关注点/历史消费备注
    notes: Optional[str] = None
    # 扩展字段：以"字段名→值"形式原样注入 prompt
    extra: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("nickname", "age", "style_preference", "scene",
                     "size_info", "budget", "notes", mode="after")
    @classmethod
    def _sanitize_text_fields(cls, v: object) -> object:
        # 自由文本：剥 HTML 标签(防 LLM 内容过滤) + lone surrogate
        return _strip_html_tags(_strip_surrogates(v))

    @field_validator("*", mode="after")
    @classmethod
    def _sanitize_str_fields(cls, v: object) -> object:
        return _strip_surrogates(v)


class SalesPitchProductInfo(BaseModel):
    """商品信息：常用结构化字段 + extra 自由扩展。"""

    sku_id: Optional[str] = None
    # 商品名称（必填）：话术中直接称呼的商品名；非空校验在清理 HTML/surrogate
    # 之后做（带 min_length 约束的 str 会在清理前拒绝 surrogate，与剥除降级
    # 策略不一致，故用裸 str + after validator）
    title: str
    price: Optional[float] = None
    # 类目（如"卫衣""运动鞋"）
    category: Optional[str] = None
    color: Optional[str] = None
    material: Optional[str] = None
    # 卖点描述（面料/工艺/功能等，逗号或分号分隔）
    selling_points: Optional[str] = None
    extra: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("sku_id", mode="after")
    @classmethod
    def _strip_sku_id(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("title", mode="after")
    @classmethod
    def _sanitize_title(cls, v: object) -> object:
        # 自由文本：剥 HTML 标签 + lone surrogate，清理后须非空
        cleaned = _strip_html_tags(_strip_surrogates(v))
        if isinstance(cleaned, str) and not cleaned.strip():
            raise ValueError("title must not be empty")
        return cleaned

    @field_validator("selling_points", mode="after")
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

    session_id: Optional[str] = None
    app_id: str
    # 顾客信息整体可选：缺省时生成通用话术
    customer: Optional[SalesPitchCustomerInfo] = None
    # 至少 1 个商品；上限防御由路由层校验
    products: List[SalesPitchProductInfo] = Field(min_length=1, max_length=10)
    # 话术风格：warm(热情亲切)/professional(专业顾问)/concise(简短干练)或自由描述
    pitch_style: Optional[str] = None
    # 触达渠道：wechat/offline/phone 等，影响排版与语气
    channel: Optional[str] = None
    # 话术字数上限（0 或缺失表示不限）
    max_length: Optional[int] = None

    @field_validator("pitch_style", "channel", mode="after")
    @classmethod
    def _strip_text(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v

    @field_validator("max_length", mode="after")
    @classmethod
    def _check_max_length(cls, v: object) -> object:
        if isinstance(v, int) and v < 0:
            raise ValueError("max_length must be >= 0")
        return v

    @field_validator("*", mode="after")
    @classmethod
    def _sanitize_str_fields(cls, v: object) -> object:
        return _strip_surrogates(v)
