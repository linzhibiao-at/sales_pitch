"""OpenAI 兼容 LLM：提示词仅从 prompt/*.md 加载。"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Any, Optional

import time

import httpx

from backend.api_debug import (
    debug_api_io_enabled,
    log_flow,
    summarize_messages_for_llm,
)
from backend.prompt_loader import load_named_prompt
from backend.config import env_or_empty, load_config, rank_outfit_limit

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


def _guess_image_mime_from_base64(b64: str) -> str:
    """从 base64 解码前缀推测图片 MIME，失败则回退为 image/jpeg。"""
    s = (b64 or "").strip()
    if not s:
        return "image/jpeg"
    sample = s[:8192]
    pad = (-len(sample)) % 4
    try:
        head = base64.b64decode(sample + ("=" * pad), validate=False)[:32]
    except (ValueError, binascii.Error):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image/gif"
    if head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _intent_current_date_prefix(today: date | None = None) -> str:
    d = today or date.today()
    return (
        f"【当前日期】{d.year}年{d.month}月{d.day}日\n"
        "（season 判断优先级：①用户文字明确提及 ②用户图片可推断 ③以上均无法判断时才按该日期所在自然季节填写；"
        "禁止 season 为空数组）\n\n"
    )


def extract_es_sku_query_json(
    user_text: str,
    *,
    image_base64: Optional[str] = None,
    model_override: str | None = None,
) -> dict[str, Any]:
    """按 sku_search_intent 提示词生成可执行 ES query 子句（或 legacy 槽位）。"""
    system = load_named_prompt("sku_search_intent")
    img = (image_base64 or "").strip()
    text_body = (user_text or "")[:4000]
    if img:
        eff_mime = _guess_image_mime_from_base64(img)
        data_uri = f"data:{eff_mime};base64,{img}"
        user_line = text_body.strip() or (
            "（用户未输入文字；请仅依据图片与系统规则生成 ES query。）"
        )
        user_content: str | list[dict[str, Any]] = [
            {"type": "text", "text": user_line},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ]
    else:
        if not text_body:
            return {}
        user_content = text_body
    content = _chat_block(
        "intent_llm",
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        temperature=0.1,
        model_override=model_override,
    )
    if not content:
        return {}
    m = re.search(r"\{[\s\S]*\}", content)
    if not m:
        return {}
    try:
        out = json.loads(m.group(0))
        return out if isinstance(out, dict) else {}
    except json.JSONDecodeError:
        return {}


def _intent_role_hint(image_role: Optional[str]) -> str:
    """图的角色提示（anchor=图即锚点 / style_ref=风格参考 / none=无图）。"""
    if image_role and image_role != "none":
        return f"【图的角色】{'style_ref' if image_role == 'style_ref' else 'anchor'}\n"
    return ""


def _build_intent_user_content(
    text_body: str,
    date_prefix: str,
    role_hint: str,
    attr_block: str,
    img: str,
) -> str | list[dict[str, Any]]:
    """构建意图解析的 user 消息内容（附图与锚点属性注入）。"""
    if img:
        eff_mime = _guess_image_mime_from_base64(img)
        data_uri = f"data:{eff_mime};base64,{img}"
        user_line = text_body.strip() or "（用户未输入文字；请仅依据图片与系统规则解析意图。）"
        blocks: list[dict[str, Any]] = []
        if attr_block:
            blocks.append({"type": "text", "text": attr_block})
        blocks.append({
            "type": "text",
            "text": date_prefix + role_hint + "【用户文字】\n" + user_line,
        })
        blocks.append({"type": "image_url", "image_url": {"url": data_uri}})
        return blocks
    # 文本路径
    user_line = text_body.strip() or "（用户未输入文字）"
    if attr_block:
        return attr_block + "\n" + date_prefix + role_hint + "【用户文字】\n" + user_line
    # 无图无属性：role_hint 此时必为空，等同 date_prefix + 纯文本
    return date_prefix + role_hint + text_body


def _parse_intent_content(content: str) -> dict[str, Any] | None:
    """从 LLM 原始输出中提取首个 JSON 对象；失败返回 None。"""
    if not content:
        return None
    m = re.search(r"\{[\s\S]*\}", content)
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        return None


def extract_intent_json(
    user_text: str,
    *,
    image_base64: Optional[str] = None,
    model_override: str | None = None,
    anchor_attr_text: Optional[str] = None,
    image_role: Optional[str] = None,
) -> dict[str, Any]:
    # category_l2 中类列表与 series 子品牌线列表已内置在 prompt/intent_extract.md
    # （§十二 CATEGORY、§十五 SERIES_LIST），不再运行时注入，避免外部数据缺失导致空枚举。
    system = load_named_prompt("intent_extract")
    date_prefix = _intent_current_date_prefix()
    img = (image_base64 or "").strip()
    text_body = (user_text or "")[:4000]
    attr_block = (anchor_attr_text or "").strip()
    role_hint = _intent_role_hint(image_role)
    user_content = _build_intent_user_content(text_body, date_prefix, role_hint, attr_block, img)
    content = _chat_block(
        "intent_llm",
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        temperature=0.1,
        model_override=model_override,
    )
    logger.info("[intent_llm_raw] 主模型原始返回:\n%s", content or "(空)")
    parsed = _parse_intent_content(content)
    return parsed or {}


def understand_image_json(
    image_base64: str,
    mime: str = "image/jpeg",
    *,
    model_override: str | None = None,
) -> dict[str, Any]:
    if not image_base64:
        return {}
    system = load_named_prompt("vision_image_understand")
    data_uri = f"data:{mime};base64,{image_base64}"
    content = _chat_block(
        "vision_llm",
        [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请解析这张图片。"},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            },
        ],
        model_override=model_override,
    )
    if not content:
        return {}
    m = re.search(r"\{[\s\S]*\}", content)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


_REASON_FALLBACK = (
    "以上搭配来自固定搭配库与文本向量临时组合，可按需挑选。"
)


def _reason_mode() -> str:
    cfg = load_config().get("recommend") or {}
    return str(cfg.get("outfit_reason_mode") or "outfit_only")


def _reason_outfit_limit() -> int:
    return rank_outfit_limit()


def _reason_max_workers() -> int:
    cfg = load_config().get("recommend") or {}
    return int(cfg.get("reason_parallel_max_workers") or 5)


def outfit_reason_key(card: dict[str, Any]) -> str:
    return str(card.get("outfit_id") or card.get("name") or "")


def _format_outfit_for_reason(card: dict[str, Any]) -> str:
    lines = [
        f"名称: {card.get('name') or card.get('outfit_id') or ''}",
        f"outfit_id: {card.get('outfit_id') or ''}",
        f"总价: {card.get('price_total')}",
        f"召回来源: {card.get('recall_source') or ''}",
    ]
    for it in card.get("items") or []:
        lines.append(
            f"- [{it.get('role')}] {it.get('title')} "
            f"({it.get('sku_id')}) ¥{it.get('price')}",
        )
    return "\n".join(lines)


def _parse_item_reasons_json(raw: str) -> dict[str, str]:
    m = re.search(r"\{[\s\S]*\}", raw or "")
    items: dict[str, str] = {}
    if not m:
        return items
    try:
        data = json.loads(m.group(0))
        for it in data.get("items") or []:
            sid = str(it.get("sku_id") or "")
            if sid:
                items[sid] = str(it.get("reason") or "")
    except json.JSONDecodeError:
        return {}
    return items


def _reason_user_block(summary: str, card: dict[str, Any]) -> str:
    from backend.dphs_reason_store import match_outfit_reasons, format_reasons_as_fewshot
    fewshot = ""
    cfg = load_config().get("recommend") or {}
    if cfg.get("reason_generation_mode", "llm") == "llm":
        matches = match_outfit_reasons(card)
        fewshot = format_reasons_as_fewshot(matches)
    base = (
        f"摘要:\n{summary}\n\n当前搭配（仅此一套）:\n"
        f"{_format_outfit_for_reason(card)}"
    )
    if fewshot:
        base = f"{base}\n\n{fewshot}"
    return base[:6000]


def _reason_outfit_only_one(
    summary: str,
    card: dict[str, Any],
    model_override: str | None = None,
) -> str:
    system = load_named_prompt("outfit_only")
    text = _chat_block(
        "reason_llm",
        [
            {"role": "system", "content": system},
            {"role": "user", "content": _reason_user_block(summary, card)},
        ],
        temperature=0.1,
        model_override=model_override,
    )
    return (text or "").strip()


def _reason_per_item_one(
    summary: str,
    card: dict[str, Any],
    model_override: str | None = None,
) -> dict[str, str]:
    system = load_named_prompt("per_item")
    user = json.dumps(
        {"summary": summary, "outfit": card},
        ensure_ascii=False,
    )[:8000]
    raw = _chat_block(
        "reason_llm",
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.1,
        model_override=model_override,
    )
    return _parse_item_reasons_json(raw or "")


def _reason_both_one(
    summary: str,
    card: dict[str, Any],
    model_override: str | None = None,
) -> tuple[str, dict[str, str]]:
    sys_o = load_named_prompt("both_outfit")
    out_o = _chat_block(
        "reason_llm",
        [
            {"role": "system", "content": sys_o},
            {"role": "user", "content": _reason_user_block(summary, card)},
        ],
        temperature=0.1,
        model_override=model_override,
    )
    narrative = (out_o or "").strip()
    sys_i = load_named_prompt("both_per_item")
    user_i = json.dumps(
        {
            "outfit_narrative": narrative,
            "outfit": card,
        },
        ensure_ascii=False,
    )[:8000]
    raw_i = _chat_block(
        "reason_llm",
        [
            {"role": "system", "content": sys_i},
            {"role": "user", "content": user_i},
        ],
        temperature=0.1,
        model_override=model_override,
    )
    return narrative, _parse_item_reasons_json(raw_i or "")


def _reason_one_outfit(
    summary: str,
    card: dict[str, Any],
    mode: str,
    model_override: str | None = None,
) -> tuple[str, str, dict[str, str]]:
    """返回 (outfit_key, outfit_reason, item_reasons)。"""
    key = outfit_reason_key(card)
    # template 模式：直接用话术库匹配，不调 LLM
    cfg = load_config().get("recommend") or {}
    if cfg.get("reason_generation_mode") == "template":
        from backend.dphs_reason_store import build_template_reason
        return key, build_template_reason(card), {}
    if mode == "outfit_only":
        return key, _reason_outfit_only_one(summary, card, model_override), {}
    if mode == "per_item":
        return key, "", _reason_per_item_one(summary, card, model_override)
    narrative, items = _reason_both_one(summary, card, model_override)
    return key, narrative, items


def generate_outfit_reason_payload(
    summary: str,
    outfit_cards: list[dict[str, Any]],
    *,
    model_override: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """按套并行调用 reason_llm，为每套搭配生成独立理由。

    ``limit`` 默认走 ``_reason_outfit_limit()``（按位置截前 N 套）；显式传入
    时按该值截取——拆分模式（partner_qwen）传整套 coarse 数量，确保 LLM
    精排后留下的幸存者一定能命中理由。
    """
    mode = _reason_mode()
    cap = limit if limit is not None else _reason_outfit_limit()
    cards = list(outfit_cards[:cap])
    outfit_reasons: dict[str, str] = {}
    item_reasons: dict[str, str] = {}

    if not cards:
        return {
            "outfit_reason": "",
            "outfit_reasons": {},
            "item_reasons": {},
        }

    workers = min(_reason_max_workers(), len(cards))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_reason_one_outfit, summary, card, mode, model_override)
            for card in cards
        ]
        for fut in as_completed(futures):
            try:
                key, outfit_r, items = fut.result()
            except Exception:
                logger.exception("parallel outfit reason failed")
                continue
            if key and outfit_r:
                outfit_reasons[key] = outfit_r
            item_reasons.update(items)

    summary_parts: list[str] = []
    for card in cards:
        key = outfit_reason_key(card)
        text = outfit_reasons.get(key, "")
        if not text:
            continue
        label = str(card.get("name") or key)
        summary_parts.append(f"【{label}】{text}")

    outfit_reason = "\n\n".join(summary_parts)
    if not outfit_reason and item_reasons:
        outfit_reason = " ".join(item_reasons.values())[:800]

    return {
        "outfit_reason": outfit_reason,
        "outfit_reasons": outfit_reasons,
        "item_reasons": item_reasons,
    }


def generate_reason_text(
    summary: str,
    outfit_titles: list[str],
    *,
    model_override: str | None = None,
) -> str:
    """兼容旧接口：等价于 outfit_only 模式根级理由。"""
    cards = [{"name": t} for t in outfit_titles]
    pay = generate_outfit_reason_payload(summary, cards, model_override=model_override)
    r = pay.get("outfit_reason") or ""
    return r or _REASON_FALLBACK
