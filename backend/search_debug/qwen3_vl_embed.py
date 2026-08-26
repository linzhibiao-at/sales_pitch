"""调用 vLLM/OpenAI 兼容的 Qwen3-VL Chat Embeddings（/v1/embeddings + messages）。"""

from __future__ import annotations

import logging
import math
import re
from typing import Any, List, Optional

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_INSTRUCT = "Represent the user's input."


def _l2_normalize(vec: List[float]) -> List[float]:
    s = math.sqrt(sum(x * x for x in vec))
    if s <= 1e-12:
        return vec
    return [x / s for x in vec]


def _base_url(base: str) -> str:
    return (base or "").strip().rstrip("/")


def _coerce_embedding(data: dict[str, Any]) -> Optional[List[float]]:
    if not isinstance(data, dict):
        return None
    block = data.get("data")
    if isinstance(block, list) and block:
        first = block[0]
        if isinstance(first, dict):
            emb = first.get("embedding")
            if isinstance(emb, list):
                out: List[float] = []
                for x in emb:
                    try:
                        out.append(float(x))
                    except (TypeError, ValueError):
                        return None
                return out if out else None
    return None


def fetch_served_model_id(
    base_url: str,
    *,
    api_key: str = "",
    timeout_sec: float = 30.0,
) -> str:
    """GET /v1/models，返回第一个 model id。"""
    base = _base_url(base_url)
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = f"{base}/v1/models"
    with httpx.Client(timeout=timeout_sec) as client:
        r = client.get(url, headers=headers)
    r.raise_for_status()
    payload = r.json()
    data = payload.get("data")
    if isinstance(data, list) and data:
        mid = (data[0] or {}).get("id")
        if isinstance(mid, str) and mid:
            return mid
    raise RuntimeError(f"/v1/models 无可用模型: {payload}")


def build_qwen3_vl_image_messages(
    *,
    image_url: str,
    text: str = "",
    instruction: str = _DEFAULT_INSTRUCT,
) -> list[dict[str, Any]]:
    """与 vLLM vision_embedding_online.run_qwen3_vl 对齐的消息结构。"""
    user_parts: list[dict[str, Any]] = [
        {"type": "image_url", "image_url": {"url": image_url}},
    ]
    user_parts.append({"type": "text", "text": text or ""})
    return [
        {
            "role": "system",
            "content": [{"type": "text", "text": instruction}],
        },
        {"role": "user", "content": user_parts},
        {"role": "assistant", "content": [{"type": "text", "text": ""}]},
    ]


def embed_with_messages(
    base_url: str,
    model: str,
    messages: list[dict[str, Any]],
    *,
    api_key: str = "",
    timeout_sec: float = 120.0,
    expected_dim: int = 0,
) -> Optional[List[float]]:
    """POST /v1/embeddings（messages 形态）。"""
    base = _base_url(base_url)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "encoding_format": "float",
        "continue_final_message": True,
        "add_special_tokens": True,
    }
    url = f"{base}/v1/embeddings"
    with httpx.Client(timeout=timeout_sec) as client:
        r = client.post(url, headers=headers, json=body)
    if r.status_code >= 400:
        logger.warning("embedding HTTP %s: %s", r.status_code, (r.text or "")[:800])
        return None
    vec = _coerce_embedding(r.json())
    if not vec:
        return None
    if expected_dim and len(vec) != expected_dim:
        logger.warning(
            "向量维度 %s 与期望 %s 不一致；请调整配置中的 dimensions",
            len(vec),
            expected_dim,
        )
        return None
    return _l2_normalize(vec)


def embed_image_url(
    base_url: str,
    model: str,
    image_url: str,
    *,
    text: str = "",
    api_key: str = "",
    timeout_sec: float = 120.0,
    expected_dim: int = 0,
) -> Optional[List[float]]:
    """单图（可选短文本）嵌入。"""
    msgs = build_qwen3_vl_image_messages(image_url=image_url, text=text)
    return embed_with_messages(
        base_url,
        model,
        msgs,
        api_key=api_key,
        timeout_sec=timeout_sec,
        expected_dim=expected_dim,
    )


def sanitize_short_text(raw: str, *, max_len: int = 512) -> str:
    """去掉多余空白，截断长度，避免把超长 keywords 塞进 embedding。"""
    s = re.sub(r"\s+", " ", (raw or "").strip())
    return s[:max_len]