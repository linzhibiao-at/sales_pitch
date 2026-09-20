"""营销话术生成服务：顾客信息 + 商品信息 → DeepAgent 导购话术 + 审计落库。"""

from __future__ import annotations

import json
import logging
from time import perf_counter
from typing import Any

from backend.config import load_config
from backend.models import SalesPitchCustomerInfo, SalesPitchProductInfo, SalesPitchRequest
from backend.services.request_audit import (
    RequestAuditLogger,
    build_sales_pitch_doc,
    now_iso,
)

logger = logging.getLogger(__name__)

# 话术风格预设 → 中文描述；未预设的取值（如"小红书种草风"）原样透传给 LLM
_PITCH_STYLE_LABELS = {
    "warm": "热情亲切",
    "professional": "专业顾问",
    "concise": "简短干练",
    # AI邀约工作台四档语气（写作定义见 .sales_pitch 提示词资产）
    "亲切自然": "亲切自然",
    "活力潮流": "活力潮流",
    "专业尊贵": "专业尊贵",
    "简约高效": "简约高效",
}

# 顾客画像字段 → 中文标签（元组顺序即注入 prompt 的顺序）
_CUSTOMER_FIELD_LABELS: tuple[tuple[str, str], ...] = (
    ("nickname", "称呼"),
    ("gender", "性别"),
    ("age", "年龄段"),
    ("member_level", "会员等级"),
    ("points", "积分"),
)

# 商品字段 → 中文标签
_PRODUCT_FIELD_LABELS: tuple[tuple[str, str], ...] = (
    ("color", "颜色"),
)


def _fmt_value(v: Any) -> str:
    """extra/自由字段值的紧凑序列化：str 原样，其余 json 化。"""
    if isinstance(v, str):
        return v.strip()
    try:
        return json.dumps(v, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(v)


def build_customer_block(customer: SalesPitchCustomerInfo | None) -> str:
    """顾客画像 →【顾客信息】文本块；无有效字段时返回空串（不注入 prompt）。"""
    if customer is None:
        return ""
    lines: list[str] = []
    for field, label in _CUSTOMER_FIELD_LABELS:
        raw = getattr(customer, field, None)
        val = _fmt_value(raw) if raw is not None else ""
        if val:
            lines.append(f"- {label}: {val}")
    for k, v in (customer.extra or {}).items():
        key = str(k).strip()
        val = _fmt_value(v)
        if key and val:
            lines.append(f"- {key}: {val}")
    if not lines:
        return ""
    return "【顾客信息】\n" + "\n".join(lines)


def build_products_block(products: list[SalesPitchProductInfo]) -> str:
    """商品清单 →【商品信息】文本块（商品名 + 颜色/extra 行）。"""
    lines: list[str] = ["【商品信息】"]
    for idx, p in enumerate(products, 1):
        lines.append(f"{idx}. {p.title}")
        for field, label in _PRODUCT_FIELD_LABELS:
            raw = getattr(p, field, None)
            val = _fmt_value(raw) if raw is not None else ""
            if val:
                lines.append(f"   - {label}: {val}")
        for k, v in (p.extra or {}).items():
            key = str(k).strip()
            val = _fmt_value(v)
            if key and val:
                lines.append(f"   - {key}: {val}")
    return "\n".join(lines)


def build_promotions_block(req: SalesPitchRequest) -> str:
    """门店已选活动 →【促销活动】文本块；缺省或空数组返回空串（不注入 prompt）。

    仅注入活动名（如有）与展示文案原文；promo_id 仅供审计溯源不进入提示词；
    话术仅可引用所提供文案（禁止编造由提示词资产约束）。
    """
    promos = req.promotions or []
    lines: list[str] = []
    for p in promos:
        name = (p.name or "").strip()
        lines.append(f"- {name}: {p.copy_text}" if name else f"- {p.copy_text}")
    if not lines:
        return ""
    return "【促销活动】\n" + "\n".join(lines)


def build_coupons_block(req: SalesPitchRequest) -> str:
    """顾客优惠券 →【顾客优惠券】文本块。

    缺省（None）返回空串——旧调用方零差异；非空列出券名；显式空列表
    （或清理后为空）输出“会员专属优惠”兜底指示（禁止编造具体券名）。
    """
    if req.coupon_names is None:
        return ""
    if req.coupon_names:
        return "【顾客优惠券】\n- 可用券: " + "、".join(req.coupon_names)
    return (
        "【顾客优惠券】\n"
        "- 顾客暂无可用券；如提及权益，必须原样使用“会员专属优惠”这一固定表述，"
        "不得改写为其他说法，不要罗列具体券名"
    )


def build_requirements_block(req: SalesPitchRequest) -> str:
    """风格/字数 →【话术要求】文本块；全部缺省时返回空串。"""
    lines: list[str] = []
    style = (req.pitch_style or "").strip()
    if style:
        lines.append(f"- 风格: {_PITCH_STYLE_LABELS.get(style.lower(), style)}")
    if req.max_length and req.max_length > 0:
        lines.append(f"- 长度: {req.max_length} 字以内")
    if not lines:
        return ""
    return "【话术要求】\n" + "\n".join(lines)


def build_extra_prompt_block(req: SalesPitchRequest) -> str:
    """导购补充要求 →【补充要求】文本块；空值返回空串（不注入 prompt）。"""
    val = (req.extra_prompt or "").strip()
    if not val:
        return ""
    return "【补充要求】\n" + val


class SalesPitchService:
    """营销话术生成：入参归一化 → 文本块 → DeepAgent → 出参 + 审计。

    ``agent`` 为 DeepAgent CompiledGraph（由 ``build_agent()`` 创建），
    ``session_id`` 映射到 LangGraph ``thread_id``，同 session 自动共享
    对话历史，``SummarizationMiddleware`` 负责上下文压缩。
    """

    def __init__(self, agent: Any) -> None:
        self._agent = agent
        self._audit = RequestAuditLogger()

    async def generate(
        self,
        req: SalesPitchRequest,
        *,
        trace_id: str | None = None,
        app_id: str | None = None,
        caller: str | None = None,
    ) -> dict[str, Any]:
        """生成话术；Agent 空输出时返回 ``{"error": ...}``（路由层转 5xx）。"""
        t0 = perf_counter()
        # session_id = {guide_num}_{union_id}：同导购同会员共享会话上下文
        session_id = f"{req.guide_num}_{req.customer.union_id}"
        status = "ok"
        error: str | None = None
        result: dict[str, Any] | None = None
        try:
            customer_block = build_customer_block(req.customer)
            products_block = build_products_block(req.products)
            promotions_block = build_promotions_block(req)
            coupons_block = build_coupons_block(req)
            requirements_block = build_requirements_block(req)
            extra_prompt_block = build_extra_prompt_block(req)
            logger.info(
                "[营销话术] 生成开始 trace_id=%s app_id=%s product_count=%d "
                "has_customer=%s style=%s",
                trace_id, app_id, len(req.products),
                bool(customer_block), req.pitch_style or "-",
            )
            # 拼装用户消息（复用现有文本块构建函数）
            # 顺序：【顾客信息】→【商品信息】→【促销活动】→【顾客优惠券】→
            #      【话术要求】→【补充要求】（空块跳过、顺序保持）
            user_msg = "\n\n".join(
                p for p in (
                    customer_block, products_block,
                    promotions_block, coupons_block,
                    requirements_block, extra_prompt_block,
                ) if p
            )
            # session_id → thread_id：同 session 自动共享对话历史
            config = {"configurable": {"thread_id": session_id}}
            agent_result = await self._agent.ainvoke(
                {"messages": [{"role": "user", "content": user_msg}]},
                config=config,
            )
            # 提取最后一条 AI 消息作为话术
            messages = agent_result.get("messages", [])
            pitch = ""
            for msg in reversed(messages):
                content = getattr(msg, "content", "") or ""
                if content and getattr(msg, "type", "") == "ai":
                    pitch = content.strip()
                    break
            if not pitch:
                status = "error"
                error = "sales pitch generation failed (empty agent output)"
                logger.error("[营销话术] Agent 空输出 trace_id=%s", trace_id)
                result = {"error": error}
                return result
            result = {
                "session_id": session_id,
                "pitch": pitch,
                "pitch_style": (req.pitch_style or "").strip(),
                "model": self._model_name(),
            }
            return result
        except Exception as exc:
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._write_audit(
                req, result, session_id, status, error,
                trace_id, app_id, caller, t0,
            )

    def _model_name(self) -> str:
        mcfg = (load_config().get("models") or {}).get("sales_pitch_llm") or {}
        primary = mcfg.get("primary") or {}
        return str(primary.get("model") or "")

    def _write_audit(
        self,
        req: SalesPitchRequest,
        result: dict[str, Any] | None,
        session_id: str,
        status: str,
        error: str | None,
        trace_id: str | None,
        app_id: str | None,
        caller: str | None,
        t0: float,
    ) -> None:
        """拼 sales_pitch 审计文档并入队（后台线程批量写 MySQL）；关闭/失败均静默。"""
        if not self._audit.enabled:
            return
        try:
            input_block = {
                "session_id": session_id,
                # 导购工号（导购身份标识）
                "guide_num": req.guide_num,
                "customer": (
                    req.customer.model_dump(exclude_none=True)
                    if req.customer is not None else None
                ),
                "products": [
                    p.model_dump(exclude_none=True) for p in req.products
                ],
                # 已选活动：未携带或空数组记 null；否则记录清理后各项
                # （copy 经 alias 序列化为接口字段名 copy）
                "promotions": (
                    [
                        p.model_dump(by_alias=True, exclude_none=True)
                        for p in req.promotions
                    ]
                    if req.promotions else None
                ),
                # 已选券：未携带记 null，显式空列表记 []（记录清理后值）
                "coupon_names": req.coupon_names,
                "pitch_style": (req.pitch_style or "").strip() or None,
                "max_length": req.max_length or None,
                "extra_prompt": (req.extra_prompt or "").strip() or None,
            }
            meta = {
                "trace_id": trace_id,
                "session_id": session_id,
                "app_id": app_id,
                "caller": caller,
                "ts": now_iso(),
                "elapsed_ms": int((perf_counter() - t0) * 1000),
                "status": status,
                "error": error,
            }
            doc = build_sales_pitch_doc(
                input_block=input_block, result=result, meta=meta,
            )
            self._audit.write(doc)
        except Exception:  # noqa: BLE001
            logger.warning("request audit (sales_pitch) failed", exc_info=True)
