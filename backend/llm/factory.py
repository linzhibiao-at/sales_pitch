"""双通道 LLM 工厂：DashScope 主 + 安踏 AI 网关 fallback。

用 LangChain 原生 ``.with_fallbacks()`` 替代原 ``llm_client.py`` 的手写
httpx 重试 + 模型链降级逻辑。DeepAgent Agent 接受 LangChain Runnable，
fallback 链完全透明。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from backend.config import env_or_empty, load_config

logger = logging.getLogger(__name__)


def _build_chat_openai(
    *,
    model: str,
    api_key: str,
    base_url: str,
    max_tokens: int = 2048,
    temperature: float | None = None,
) -> Any:
    """构造 ChatOpenAI 实例（DashScope / 安踏网关均兼容 OpenAI 协议）。

    当 ``api_key`` 为空时，使用占位符让构造通过；实际调用时会返回鉴权错误，
    由上层 fallback 链或异常处理器捕获。
    """
    from langchain_openai import ChatOpenAI

    if not api_key:
        logger.warning(
            "[llm] api_key 为空（model=%s, base=%s），使用占位符；实际调用将失败",
            model, base_url[:40],
        )
        api_key = "placeholder"

    kw: dict[str, Any] = {
        "model": model,
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        kw["temperature"] = temperature
    return ChatOpenAI(**kw)


def create_sales_pitch_llm() -> Any:
    """创建话术生成 LLM。

    主通道：DashScope（``config.yaml`` → ``models.sales_pitch_llm.primary``）。

    DeepAgent 的 ``create_deep_agent()`` 要求 ``BaseChatModel``，
    不接受 ``RunnableWithFallbacks``；fallback 降级由 Service 层处理。

    Returns:
        ChatOpenAI 实例，可直接传给 ``create_deep_agent(model=...)``。
    """
    cfg = load_config()
    mcfg = (cfg.get("models") or {}).get("sales_pitch_llm") or {}

    primary_cfg = mcfg.get("primary") or {}
    fallback_cfg = mcfg.get("fallback") or {}

    primary_key = env_or_empty(str(primary_cfg.get("api_key_env") or "DASHSCOPE_API_KEY"))
    primary = _build_chat_openai(
        model=str(primary_cfg.get("model") or "qwen-plus"),
        api_key=primary_key,
        base_url=str(
            primary_cfg.get("base_url")
            or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ),
        max_tokens=int(primary_cfg.get("max_tokens") or 2048),
    )

    # fallback 模型仅创建不包装（DeepAgent 不接受 RunnableWithFallbacks）
    fb_key = env_or_empty(str(fallback_cfg.get("api_key_env") or "ANTA_LLM_API_KEY"))
    _fb = _build_chat_openai(
        model=str(fallback_cfg.get("model") or "qwen3.5-flash"),
        api_key=fb_key,
        base_url=str(
            fallback_cfg.get("base_url")
            or "https://ai.anta.com/aimodels-server/private/llm/v1"
        ),
        max_tokens=int(fallback_cfg.get("max_tokens") or 2048),
    )
    # 挂载到 primary 实例属性，供 Service 层降级使用
    primary._fallback_model = _fb  # type: ignore[attr-defined]

    logger.info(
        "[llm] 双通道 LLM: primary=%s (%s), fallback=%s (%s)",
        primary_cfg.get("model"), primary_cfg.get("base_url", "")[:40],
        fallback_cfg.get("model"), fallback_cfg.get("base_url", "")[:40],
    )
    return primary


def create_summarization_llm() -> Any:
    """创建上下文压缩专用 LLM（用便宜的小模型）。

    读取 ``models.sales_pitch_llm.summarization.model``，
    走 DashScope 通道（压缩任务不需要 fallback）。
    """
    from backend.config import get_summarization_config

    sc = get_summarization_config()
    cfg = load_config()
    mcfg = (cfg.get("models") or {}).get("sales_pitch_llm") or {}
    primary_cfg = mcfg.get("primary") or {}

    # 复用主通道的 API Key 和 base_url
    api_key = env_or_empty(str(primary_cfg.get("api_key_env") or "DASHSCOPE_API_KEY"))
    base_url = str(
        primary_cfg.get("base_url")
        or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )

    return _build_chat_openai(
        model=sc["model"],
        api_key=api_key,
        base_url=base_url,
    )
