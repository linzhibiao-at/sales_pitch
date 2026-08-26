"""Doubao / 火山 Ark 多模态图像向量（与 fila_agent_html embedding 逻辑对齐的精简版）。"""

from __future__ import annotations

import logging
import math
import os
import time
import uuid
from typing import Any, List, Optional

import httpx

logger = logging.getLogger(__name__)

_ARK_DEFAULT_API_V3_BASE = "https://ark.cn-beijing.volces.com/api/v3"


def _coerce_embedding_vec(raw: Any) -> Optional[List[float]]:
    if raw is None:
        return None
    if hasattr(raw, "tolist"):
        try:
            raw = raw.tolist()
        except Exception:
            return None
    if not isinstance(raw, list):
        return None
    out: List[float] = []
    for x in raw:
        try:
            out.append(float(x))
        except (TypeError, ValueError):
            return None
    return out if out else None


def _extract_embedding_from_json(data: dict[str, Any]) -> Optional[Any]:
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("embedding"), list):
        return data["embedding"]
    block = data.get("data")
    if isinstance(block, list) and block:
        first = block[0]
        if isinstance(first, dict):
            if isinstance(first.get("embedding"), list):
                return first["embedding"]
            emb = first.get("embedding")
            if emb is not None:
                return emb
    for key in ("result", "output"):
        sub = data.get(key)
        if isinstance(sub, dict) and isinstance(sub.get("embedding"), list):
            return sub["embedding"]
    return None


def _l2_normalize(vec: List[float]) -> List[float]:
    s = math.sqrt(sum(x * x for x in vec))
    if s <= 1e-12:
        return vec
    return [x / s for x in vec]


def _ark_sdk_available() -> bool:
    try:
        import volcenginesdkarkruntime  # noqa: F401
        return True
    except ImportError:
        return False


def _native_ark_embedding_base(base: str) -> bool:
    if os.environ.get("EMBEDDING_FORCE_HTTPX", "").lower() in (
        "1",
        "true",
        "yes",
    ):
        return False
    bl = base.lower()
    if "openai" in bl or bl.endswith("/v1") or "/v1/" in bl:
        return False
    return True


def _normalize_and_validate_embedding(
    raw_emb: Any,
    dims: int,
) -> tuple[Optional[List[float]], str]:
    vec = _coerce_embedding_vec(raw_emb)
    if not vec:
        return None, "no embedding in response"
    got = len(vec)
    if got != dims:
        if got > dims:
            vec = vec[:dims]
        else:
            return None, f"short embedding len={got} need={dims}"
    return _l2_normalize(vec), ""


def _build_ark_sdk_client(api_key: str, base: str):
    from volcenginesdkarkruntime import Ark

    if base.rstrip("/") == _ARK_DEFAULT_API_V3_BASE.rstrip("/"):
        return Ark(api_key=api_key)
    return Ark(api_key=api_key, base_url=base)


def _embed_multimodal_via_ark_sdk(
    url: str,
    api_key: str,
    base: str,
    model: str,
    encoding_format: str,
    dims: int,
    omit_dim: bool,
    cfg_has_dimensions: bool,
    timeout: float,
) -> Optional[List[float]]:
    ark = _build_ark_sdk_client(api_key, base)
    input_parts = [
        {"type": "image_url", "image_url": {"url": url}},
    ]
    kwargs: dict[str, Any] = {
        "model": model,
        "encoding_format": encoding_format or "float",
        "input": input_parts,
        "timeout": timeout,
    }
    if not omit_dim and cfg_has_dimensions:
        kwargs["dimensions"] = dims
    resp = ark.multimodal_embeddings.create(**kwargs)
    emb = resp.data.embedding
    if hasattr(emb, "flatten"):
        emb = emb.flatten().tolist()
    elif hasattr(emb, "tolist"):
        emb = emb.tolist()
    elif not isinstance(emb, list):
        emb = list(emb)
    return emb


def embed_image_url(emb_cfg: dict[str, Any], image_url: str) -> Optional[List[float]]:
    """使用 ``emb_cfg`` 调用 Ark 多模态嵌入；``image_url`` 可为 http(s) 或 data URI。"""
    dims = int(emb_cfg.get("dimensions") or 1024)
    model = str(emb_cfg.get("model") or "doubao-embedding-vision-251215")
    timeout = float(emb_cfg.get("timeout_sec") or 120)
    retries = int(emb_cfg.get("max_retries") or 2)
    delay = float(emb_cfg.get("retry_delay_sec") or 2)
    key_env = str(emb_cfg.get("api_key_env") or "ARK_API_KEY")
    inline_key = str(emb_cfg.get("api_key") or "").strip()
    api_key = inline_key or os.environ.get(key_env, "") or os.environ.get(
        "ARK_API_KEY",
        "",
    )
    base = str(
        emb_cfg.get("base_url")
        or os.environ.get("EMBEDDING_BASE_URL", _ARK_DEFAULT_API_V3_BASE),
    ).rstrip("/")
    if not api_key:
        logger.warning("未设置 %s / ARK_API_KEY，无法请求 Doubao 向量", key_env)
        return None
    path = "/embeddings/multimodal"
    if "openai" in base or "/v1" in base:
        path = "/embeddings"
    encoding_format = str(emb_cfg.get("encoding_format") or "float")
    body: dict[str, Any] = {
        "model": model,
        "encoding_format": encoding_format,
        "input": [
            {"type": "image_url", "image_url": {"url": image_url}},
        ],
    }
    omit_dim = os.environ.get(
        "EMBEDDING_OMIT_REQUEST_DIMENSIONS",
        "",
    ).lower() in ("1", "true", "yes")
    cfg_has_dimensions = emb_cfg.get("dimensions") is not None
    if not omit_dim and cfg_has_dimensions:
        body["dimensions"] = dims
    want_sdk = _native_ark_embedding_base(base)
    use_sdk = want_sdk and _ark_sdk_available()
    last_err: Optional[str] = None
    for attempt in range(retries + 1):
        try:
            raw_emb: Any = None
            if use_sdk and _ark_sdk_available():
                try:
                    raw_emb = _embed_multimodal_via_ark_sdk(
                        url=image_url,
                        api_key=api_key,
                        base=base,
                        model=model,
                        encoding_format=encoding_format,
                        dims=dims,
                        omit_dim=omit_dim,
                        cfg_has_dimensions=cfg_has_dimensions,
                        timeout=timeout,
                    )
                except Exception as exc:
                    last_err = f"ark sdk: {exc}"
                    logger.warning("Ark SDK 多模态失败，尝试 HTTP: %s", exc)
                    raw_emb = None
            if raw_emb is None:
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "volc-sdk-python/1.0.0",
                    "X-Client-Request-Id": str(uuid.uuid4()),
                }
                with httpx.Client(timeout=timeout) as client:
                    r = client.post(
                        f"{base}{path}",
                        headers=headers,
                        json=body,
                    )
                if r.status_code >= 400:
                    last_err = f"HTTP {r.status_code}: {r.text[:500]}"
                    time.sleep(delay)
                    continue
                data = r.json()
                raw_emb = _extract_embedding_from_json(data)
            out, norm_err = _normalize_and_validate_embedding(raw_emb, dims)
            if out is not None:
                return out
            last_err = last_err or norm_err
            time.sleep(delay)
            continue
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            last_err = str(exc)
        time.sleep(delay)
    logger.warning("Doubao embedding failed: %s", last_err)
    return None