"""多模态推荐理由：本地部署 qwen3.6-27b（``models.vision_llm``）看搭配单品
``tryon_image`` 生成推荐理由（**逐套并行**）。

调用方式参考 ``scripts/fila_images_preprocess.py``（OpenAI SDK + ``vision_llm``
配置 + ``_call_vlm``），但**不下载/resize 图片**：直接把每件单品的 ``tryon_image``
url 作为 ``image_url`` 块发给 VLM，由模型侧自行取图（与 partner vLLM 一致）。

返回结构与 ``backend.llm_client.generate_outfit_reason_payload`` 对齐：
``{outfit_reason, outfit_reasons, item_reasons}``，供 ``chat_stream`` 的拆分并行
分支（``llm_rank_reason_method=partner_vision``）按 ``outfit_reason_key`` 写回。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List

from backend.llm_client import (  # noqa: PLC0415
    _reason_max_workers,
    _reason_outfit_limit,
    _reason_user_block,
    outfit_reason_key,
)
from backend.prompt_loader import load_named_prompt
from backend.config import env_or_empty, load_config

# 复用 fila_images_preprocess 的 OpenAI SDK 客户端构建 + 通用 VLM 调用 +
# vision_llm 配置解析（单一事实源）。
from scripts.fila_images_preprocess import (  # noqa: PLC0415
    _call_vlm,
    _create_chat_client,
    resolve_vision_llm_settings,
)

logger = logging.getLogger(__name__)

# 单套搭配送 VLM 的图片上限（与 partner V6 的 5 图截断一致）
_MAX_IMAGES_PER_OUTFIT = 5


def _resolve_vision_reason_settings() -> Dict[str, Any]:
    """读取推荐理由专用 VLM 配置：``recommend.vision_reason_llm`` 覆盖
    ``models.vision_llm`` 的 base_url（独立部署实例），其余字段继承 vision_llm。"""
    settings = resolve_vision_llm_settings()
    vrl = (load_config().get("recommend") or {}).get("vision_reason_llm") or {}
    if vrl.get("base_url"):
        settings["api_base"] = str(vrl["base_url"]).strip().rstrip("/")
    if vrl.get("model"):
        settings["model"] = str(vrl["model"])
    if vrl.get("api_key_env"):
        key = env_or_empty(str(vrl["api_key_env"]))
        if key:
            settings["api_key"] = key
            settings["api_key_env"] = str(vrl["api_key_env"])
    if vrl.get("timeout_sec") is not None:
        settings["timeout_sec"] = float(vrl["timeout_sec"])
    if vrl.get("max_tokens") is not None:
        settings["max_tokens"] = int(vrl["max_tokens"])
    if "enable_thinking" in vrl:
        settings["enable_thinking"] = (
            bool(vrl["enable_thinking"]) if vrl["enable_thinking"] is not None else None
        )
    return settings


def _outfit_image_urls(card: Dict[str, Any]) -> List[str]:
    """收集搭配单品的 tryon_image url（缺失时回退 display_image），去重、≤5 张。"""
    urls: List[str] = []
    for it in card.get("items") or []:
        u = str(it.get("tryon_image") or it.get("display_image") or "").strip()
        if u and u not in urls:
            urls.append(u)
        if len(urls) >= _MAX_IMAGES_PER_OUTFIT:
            break
    return urls


def _build_url_image_content(urls: List[str]) -> List[Dict[str, Any]]:
    """直接用 url 构造 OpenAI 兼容 image_url 块（不下载/resize）。"""
    return [{"type": "image_url", "image_url": {"url": u}} for u in urls]


def _vision_reason_one(
    summary: str,
    card: Dict[str, Any],
    settings: Dict[str, Any],
) -> tuple[str, str]:
    """对单套搭配调用 qwen3.6-27b，返回 (outfit_key, reason_text)。"""
    key = outfit_reason_key(card)
    urls = _outfit_image_urls(card)
    image_content = _build_url_image_content(urls) if urls else []
    system = load_named_prompt("outfit_only")
    user_text = _reason_user_block(summary, card)
    logger.info(
        "[vision_reason_in] model=%s base=%s outfit=%s images=%d",
        settings.get("model"), settings.get("api_base"), key, len(urls),
    )
    client = _create_chat_client(
        settings["api_base"],
        settings["api_key"],
        settings["timeout_sec"],
    )
    raw = _call_vlm(
        client=client,
        model=settings["model"],
        system_prompt=system,
        user_text=user_text,
        image_content=image_content,
        max_tokens=settings["max_tokens"],
        enable_thinking=settings["enable_thinking"],
    )
    logger.info(
        "[vision_reason_out] outfit=%s reason_len=%d",
        key, len(raw or ""),
    )
    return key, (raw or "").strip()


def generate_outfit_reason_payload_vision(
    summary: str,
    outfit_cards: List[Dict[str, Any]],
    *,
    model_override: str | None = None,
    limit: int | None = None,
) -> Dict[str, Any]:
    """逐套并行调用 qwen3.6-27b 看图生成推荐理由。

    ``limit`` 默认走 ``_reason_outfit_limit()``；拆分模式传整套 coarse 数量，
    保证精排后幸存者都能命中理由（与 ``generate_outfit_reason_payload`` 一致）。

    ``model_override`` 为签名对齐保留并**忽略**：VLM 模型固定走 config
    ``models.vision_llm``（qwen3.6-27b），不接受文本模型名覆盖。
    """
    cap = limit if limit is not None else _reason_outfit_limit()
    cards = list(outfit_cards[:cap])
    if not cards:
        return {"outfit_reason": "", "outfit_reasons": {}, "item_reasons": {}}

    settings = _resolve_vision_reason_settings()
    workers = min(_reason_max_workers(), len(cards))
    outfit_reasons: Dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(_vision_reason_one, summary, c, settings): c
            for c in cards
        }
        for fut in as_completed(futs):
            try:
                key, text = fut.result()
            except Exception:
                logger.exception("vision_reasoner: 单套理由生成失败")
                continue
            if key and text:
                outfit_reasons[key] = text

    summary_parts: List[str] = []
    for c in cards:
        key = outfit_reason_key(c)
        text = outfit_reasons.get(key, "")
        if not text:
            continue
        label = str(c.get("name") or key)
        summary_parts.append(f"【{label}】{text}")
    outfit_reason = "\n\n".join(summary_parts)

    return {
        "outfit_reason": outfit_reason,
        "outfit_reasons": outfit_reasons,
        "item_reasons": {},
    }


__all__ = ["generate_outfit_reason_payload_vision"]
