"""OpenAI 兼容 LLM 客户端：提示词仅从 prompt/*.md 加载。"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from backend.api_debug import (
    debug_api_io_enabled,
    log_flow,
    summarize_messages_for_llm,
)
from backend.prompt_loader import load_named_prompt
from backend.config import env_or_empty, load_config

logger = logging.getLogger(__name__)

# ---------- shared httpx client (connection pool reuse) ----------
_llm_http_pool: httpx.Client | None = None
_llm_http_pool_lock = __import__("threading").Lock()


def _get_llm_http_client(timeout: float) -> httpx.Client:
    """Return a process-wide httpx.Client for LLM calls."""
    global _llm_http_pool
    with _llm_http_pool_lock:
        if _llm_http_pool is None or _llm_http_pool.is_closed:
            _llm_http_pool = httpx.Client(timeout=timeout)
        return _llm_http_pool


def _llm_post_retry(
    section: str,
    url: str,
    headers: dict[str, Any],
    body: dict[str, Any],
    timeout: float,
    retries: int,
    retry_delay: float,
) -> tuple[dict[str, Any] | None, str | None]:
    """对单模型做最多 retries+1 次尝试。

    成功返回 (parsed_json, None)；失败返回 (None, last_err)。
    4xx（非 429）视为不可重试，立即放弃该模型；429/5xx/超时/网络错误重试。
    """
    model = body.get("model")
    last_err: str | None = None
    for attempt in range(retries + 1):
        try:
            client = _get_llm_http_client(timeout)
            r = client.post(url, headers=headers, json=body)
            if r.status_code >= 400:
                err_snippet = (r.text or "")[:500]
                last_err = f"HTTP {r.status_code}: {err_snippet}"
                # 4xx（非 429）通常是请求本身有问题，重试无意义；交给上层换模型
                if r.status_code != 429 and r.status_code < 500:
                    logger.warning(
                        "llm_client[%s][%s] 不可重试错误 %d: %s",
                        section, model, r.status_code, err_snippet,
                    )
                    return None, last_err
                logger.warning(
                    "llm_client[%s][%s] 第%d次请求失败 HTTP %d，%.1fs 后重试",
                    section, model, attempt + 1, r.status_code, retry_delay,
                )
                time.sleep(retry_delay)
                continue
            return r.json(), None
        except httpx.TimeoutException as e:
            last_err = f"timeout: {e}"
            logger.warning(
                "llm_client[%s][%s] 第%d次请求超时，%.1fs 后重试",
                section, model, attempt + 1, retry_delay,
            )
            time.sleep(retry_delay)
            continue
        except httpx.HTTPError as e:
            last_err = f"http error: {e}"
            logger.warning(
                "llm_client[%s][%s] 第%d次请求网络错误: %s，%.1fs 后重试",
                section, model, attempt + 1, e, retry_delay,
            )
            time.sleep(retry_delay)
            continue
    return None, last_err


def _chat_block(
    section: str,
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.1,
    model_override: str | None = None,
) -> str:
    cfg = load_config()
    mcfg = (cfg.get("models") or {}).get(section) or {}
    base = (mcfg.get("base_url") or "").rstrip("/")
    key_env = mcfg.get("api_key_env") or "OPENAI_API_KEY"
    timeout = float(mcfg.get("timeout_sec") or 60)
    api_key = env_or_empty(key_env) or os.environ.get("OPENAI_API_KEY", "")
    if not base or not api_key:
        logger.warning("llm_client: missing base_url or api_key for %s", section)
        return ""
    url = f"{base}/chat/completions"
    enable_thinking = mcfg.get("enable_thinking")
    max_tokens = mcfg.get("max_tokens")
    retries = int(mcfg.get("max_retries") or 2)
    retry_delay = float(mcfg.get("retry_delay_sec") or 2)

    # 模型链：主模型重试耗尽后，按 fallback_models 依次换模型再调
    primary = model_override or mcfg.get("model") or "gpt-4o-mini"
    fb_cfg = mcfg.get("fallback_models") or []
    if not isinstance(fb_cfg, list):
        fb_cfg = []
    fallback_models = [m for m in fb_cfg if m and m != primary]
    model_chain = [primary, *fallback_models]

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if debug_api_io_enabled():
        host = ""
        if "//" in base:
            host = base.split("//", 1)[-1].split("/", 1)[0]
        else:
            host = base[:80]
        log_flow(
            "llm_call_in",
            {
                "section": section,
                "model": primary,
                "fallback_models": fallback_models,
                "base_host": host,
                "temperature": temperature,
                "messages": summarize_messages_for_llm(messages),
            },
        )

    last_err: str | None = None
    for idx, model in enumerate(model_chain):
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            body["max_tokens"] = int(max_tokens)
        if enable_thinking is not None:
            body["enable_thinking"] = bool(enable_thinking)
        data, last_err = _llm_post_retry(
            section, url, headers, body, timeout, retries, retry_delay,
        )
        if data is not None:
            raw = (
                (data.get("choices") or [{}])[0]
                .get("message", {})
                .get("content", "")
                or ""
            )
            if debug_api_io_enabled():
                log_flow(
                    "llm_call_out",
                    {
                        "section": section,
                        "model": model,
                        "content_len": len(raw),
                        "content_preview": (raw[:600] if raw else ""),
                    },
                )
            return raw
        if idx < len(model_chain) - 1:
            logger.warning(
                "llm_client[%s] 模型 %s 重试耗尽(%s)，切换到 fallback 模型 %s",
                section, model, last_err, model_chain[idx + 1],
            )
    logger.error(
        "llm_client[%s] 所有模型重试后仍失败(链=%s): %s",
        section, model_chain, last_err,
    )
    return ""


def generate_sales_pitch(
    customer_block: str,
    products_block: str,
    requirements_block: str,
    *,
    model_override: str | None = None,
) -> str:
    """按 sales_pitch 提示词生成营销话术。

    三个 *_block 为 service 层拼装好的中文文本段（顾客画像 / 商品清单 /
    话术要求），空段自动跳过；返回话术正文（失败返空串，由上层降级）。
    话术为创意生成任务，temperature 高于抽取/排序类调用。
    """
    system = load_named_prompt("sales_pitch")
    user = "\n\n".join(
        p for p in (customer_block, products_block, requirements_block) if p
    )[:6000]
    content = _chat_block(
        "sales_pitch_llm",
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.7,
        model_override=model_override,
    )
    return (content or "").strip()
