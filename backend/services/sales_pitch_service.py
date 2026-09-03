"""营销话术生成服务：顾客信息 + 商品信息 → DeepAgent 导购话术 + 审计落库。"""

from __future__ import annotations

import json
import logging
import uuid
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
}

# 触达渠道预设 → 中文描述；未知渠道原样透传
_CHANNEL_LABELS = {
    "wechat": "微信",
    "offline": "线下门店",
    "phone": "电话",
    "live": "直播",
}

# 顾客画像字段 → 中文标签（元组顺序即注入 prompt 的顺序）
_CUSTOMER_FIELD_LABELS: tuple[tuple[str, str], ...] = (
    ("nickname", "称呼"),
    ("gender", "性别"),
    ("age", "年龄段"),
    ("style_preference", "风格偏好"),
    ("scene", "使用场景"),
    ("size_info", "尺码信息"),
    ("budget", "预算"),
    ("notes", "备注"),
)

# 商品字段 → 中文标签
_PRODUCT_FIELD_LABELS: tuple[tuple[str, str], ...] = (
    ("category", "类目"),
    ("color", "颜色"),
    ("material", "材质"),
    ("selling_points", "卖点"),
)


def _fmt_value(v: Any) -> str:
    """extra/自由字段值的紧凑序列化：str 原样，其余 json 化。"""
    if isinstance(v, str):
        return v.strip()
    try:
        return json.dumps(v, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(v)


def _fmt_price(v: float) -> str:
    """价格显示：整数去掉小数点（¥399 而非 ¥399.0）。"""
    f = float(v)
    return f"¥{int(f)}" if f.is_integer() else f"¥{f}"


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
    """商品清单 →【商品信息】文本块；货号仅供溯源不入话术（prompt 禁止项）。"""
    lines: list[str] = ["【商品信息】"]
    for idx, p in enumerate(products, 1):
        head = f"{idx}. {p.title}"
        if p.sku_id:
            head += f"（货号 {p.sku_id}）"
        lines.append(head)
        if p.price is not None:
            lines.append(f"   - 价格: {_fmt_price(p.price)}")
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


def build_requirements_block(req: SalesPitchRequest) -> str:
    """风格/渠道/字数 →【话术要求】文本块；全部缺省时返回空串。"""
    lines: list[str] = []
    style = (req.pitch_style or "").strip()
    if style:
        lines.append(f"- 风格: {_PITCH_STYLE_LABELS.get(style.lower(), style)}")
    channel = (req.channel or "").strip()
    if channel:
        lines.append(f"- 渠道: {_CHANNEL_LABELS.get(channel.lower(), channel)}")
    if req.max_length and req.max_length > 0:
        lines.append(f"- 长度: {req.max_length} 字以内")
    if not lines:
        return ""
    return "【话术要求】\n" + "\n".join(lines)


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
        session_id = (req.session_id or "").strip() or uuid.uuid4().hex
        status = "ok"
        error: str | None = None
        result: dict[str, Any] | None = None
        try:
            customer_block = build_customer_block(req.customer)
            products_block = build_products_block(req.products)
            requirements_block = build_requirements_block(req)
            logger.info(
                "[营销话术] 生成开始 trace_id=%s app_id=%s product_count=%d "
                "has_customer=%s style=%s channel=%s",
                trace_id, app_id, len(req.products),
                bool(customer_block), req.pitch_style or "-", req.channel or "-",
            )
            # 拼装用户消息（复用现有文本块构建函数）
            user_msg = "\n\n".join(
                p for p in (customer_block, products_block, requirements_block) if p
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
                "customer": (
                    req.customer.model_dump(exclude_none=True)
                    if req.customer is not None else None
                ),
                "products": [
                    p.model_dump(exclude_none=True) for p in req.products
                ],
                "pitch_style": (req.pitch_style or "").strip() or None,
                "channel": (req.channel or "").strip() or None,
                "max_length": req.max_length or None,
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
