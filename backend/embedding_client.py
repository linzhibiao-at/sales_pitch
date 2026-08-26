"""多模态向量：优先 Ark SDK，失败则返回空向量占位（跳过 Milvus）。"""

from __future__ import annotations

import base64
import hashlib
import logging
import math
import os
import threading
import time
import uuid
from typing import Any, List, Optional

import httpx

from backend.api_debug import debug_api_io_enabled, log_flow
from backend.config import env_or_empty, load_config

logger = logging.getLogger(__name__)

# ---------- embedding result cache ----------
_EMBED_CACHE_MAX = 500
_EMBED_CACHE_TTL = 1800  # 30 min
_embed_cache: dict[str, tuple[float, list[float]]] = {}
_embed_cache_lock = threading.Lock()


def _cache_get(key: str) -> list[float] | None:
    with _embed_cache_lock:
        entry = _embed_cache.get(key)
        if entry is None:
            return None
        ts, vec = entry
        if time.monotonic() - ts > _EMBED_CACHE_TTL:
            del _embed_cache[key]
            return None
        return vec


def _cache_put(key: str, vec: list[float]) -> None:
    with _embed_cache_lock:
        if len(_embed_cache) >= _EMBED_CACHE_MAX:
            oldest_key = min(_embed_cache, key=lambda k: _embed_cache[k][0])
            del _embed_cache[oldest_key]
        _embed_cache[key] = (time.monotonic(), vec)

# 与 volcenginesdkarkruntime._constants.BASE_URL 保持一致（默认北京地域 v3）
_ARK_DEFAULT_API_V3_BASE = "https://ark.cn-beijing.volces.com/api/v3"

# ---------- shared httpx client (connection pool reuse) ----------
_http_pool: httpx.Client | None = None
_http_pool_lock = __import__("threading").Lock()


def _get_http_client(timeout: float) -> httpx.Client:
    """Return a process-wide httpx.Client, reusing TCP/TLS connections.

    httpx.Client is thread-safe; we lazily create one and recreate if
    the timeout changes significantly or the client was closed.
    """
    global _http_pool
    with _http_pool_lock:
        if _http_pool is None or _http_pool.is_closed:
            _http_pool = httpx.Client(timeout=timeout)
        return _http_pool

_missing_key_warned = False
_sdk_missing_warned = False
_dim_mismatch_warn_count = 0
_DIM_MISMATCH_WARN_MAX = 5


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


def _log_ark_multimodal_404_hint(*, base: str, path: str) -> None:
    logger.error(
        "Ark 返回 404（%s%s）：请核对 (1) embedding.model 是否为方舟控制台"
        "「推理接入点 ID」（通常 ep- 开头），而非仅写展示用模型名；"
        "(2) ARK_API_KEY 与该接入点属同一账号/项目；"
        "(3) 地域与基址一致（默认北京 api/v3）。"
        "与 descente 脚本共用同一密钥时，模型参数也应一致。",
        base,
        path,
    )


def _maybe_log_ark_404_hint(exc: BaseException, *, base: str, path: str) -> None:
    try:
        from volcenginesdkarkruntime._exceptions import ArkNotFoundError

        if isinstance(exc, ArkNotFoundError):
            _log_ark_multimodal_404_hint(base=base, path=path)
    except ImportError:
        return


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
    """是否应使用火山 Ark 原生 HTTP 形态（非 OpenAI 兼容 /v1 代理）。"""
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
    model: str,
    base: str,
    path: str,
) -> tuple[Optional[List[float]], str]:
    """将原始 embedding 校验维度、L2 归一化。"""
    global _dim_mismatch_warn_count

    vec = _coerce_embedding_vec(raw_emb)
    if not vec:
        return None, "no embedding in response"
    got = len(vec)
    if got != dims:
        if got > dims:
            if _dim_mismatch_warn_count < _DIM_MISMATCH_WARN_MAX:
                logger.warning(
                    "向量长度 %d > 配置 dimensions=%d，将截断后写入 "
                    "（建议在 config.yaml 将 embedding.dimensions 改为 %d "
                    "并重建 Milvus）",
                    got,
                    dims,
                    got,
                )
                _dim_mismatch_warn_count += 1
            vec = vec[:dims]
        else:
            if _dim_mismatch_warn_count < _DIM_MISMATCH_WARN_MAX:
                logger.warning(
                    "向量长度 %d < 配置 dimensions=%d，无法写入 Milvus；"
                    "请把 config.yaml 中 embedding.dimensions 改为 %d "
                    "并删除集合后重建索引",
                    got,
                    dims,
                    got,
                )
                _dim_mismatch_warn_count += 1
            return None, f"short embedding len={got} need={dims}"
    out = _l2_normalize(vec)
    if debug_api_io_enabled():
        log_flow(
            "embedding_call",
            {
                "base": base[:96],
                "path": path,
                "model": model,
                "ok": True,
                "dim": len(out),
            },
        )
    return out, ""


def _build_ark_sdk_client(api_key: str, base: str):
    """与 scripts/build_descente_milvus_index.py 一致：默认基址不传 base_url。"""
    from volcenginesdkarkruntime import Ark

    if base.rstrip("/") == _ARK_DEFAULT_API_V3_BASE.rstrip("/"):
        return Ark(api_key=api_key)
    return Ark(api_key=api_key, base_url=base)


def _embed_multimodal_via_ark_sdk(
    input_parts: list[dict[str, Any]],
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


def _embed_multimodal_parts(
    input_parts: list[dict[str, Any]],
    *,
    modality: str = "multimodal",
) -> Optional[List[float]]:
    """Doubao 多模态向量：image / text 共用同一 Ark 接口。"""
    global _missing_key_warned, _sdk_missing_warned

    if not input_parts:
        return None
    cfg = load_config()
    emb_cfg = cfg.get("embedding") or {}
    dims = int(emb_cfg.get("dimensions") or 1024)
    model = emb_cfg.get("model") or "doubao-embedding-vision-251215"
    timeout = float(emb_cfg.get("timeout_sec") or 120)
    retries = int(emb_cfg.get("max_retries") or 2)
    delay = float(emb_cfg.get("retry_delay_sec") or 2)
    key_env = emb_cfg.get("api_key_env") or "ARK_API_KEY"
    api_key = env_or_empty(key_env) or os.environ.get("ARK_API_KEY", "")
    base = os.environ.get(
        "EMBEDDING_BASE_URL",
        _ARK_DEFAULT_API_V3_BASE,
    ).rstrip("/")
    if not api_key:
        if not _missing_key_warned:
            logger.warning(
                "未设置 %s / ARK_API_KEY，无法请求向量；请在环境中配置密钥",
                key_env,
            )
            _missing_key_warned = True
        return None
    path = "/embeddings/multimodal"
    if "openai" in base or "/v1" in base:
        path = "/embeddings"
    encoding_format = str(emb_cfg.get("encoding_format") or "float")
    body: dict[str, Any] = {
        "model": model,
        "encoding_format": encoding_format,
        "input": input_parts,
    }
    omit_dim = os.environ.get(
        "EMBEDDING_OMIT_REQUEST_DIMENSIONS", "",
    ).lower() in ("1", "true", "yes")
    cfg_has_dimensions = emb_cfg.get("dimensions") is not None
    if not omit_dim and cfg_has_dimensions:
        body["dimensions"] = dims
    want_sdk = _native_ark_embedding_base(base)
    use_sdk = want_sdk and _ark_sdk_available()
    if want_sdk and not _ark_sdk_available() and not _sdk_missing_warned:
        logger.warning(
            "未安装 volcengine-python-sdk[ark]，将仅用 httpx 调用 Ark；"
            "若遇 HTTP 404，请执行: pip install \"volcengine-python-sdk[ark]\" "
            "（与 scripts/build_descente_milvus_index.py 一致）",
        )
        _sdk_missing_warned = True
    last_err: Optional[str] = None
    for attempt in range(retries + 1):
        try:
            raw_emb: Any = None
            if use_sdk and _ark_sdk_available():
                try:
                    raw_emb = _embed_multimodal_via_ark_sdk(
                        input_parts,
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
                    logger.warning(
                        "Ark SDK 多模态向量化失败，将尝试 HTTP 回退: %s",
                        exc,
                    )
                    _maybe_log_ark_404_hint(exc, base=base, path=path)
                    raw_emb = None
            if raw_emb is None:
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "volc-sdk-python/1.0.0",
                    "X-Client-Request-Id": str(uuid.uuid4()),
                }
                client = _get_http_client(timeout)
                r = client.post(
                    f"{base}{path}",
                    headers=headers,
                    json=body,
                )
                if r.status_code >= 400:
                    last_err = f"HTTP {r.status_code}: {r.text[:500]}"
                    if r.status_code == 404:
                        _log_ark_multimodal_404_hint(base=base, path=path)
                        snippet = (r.text or "").strip()
                        if snippet:
                            logger.error(
                                "Ark 多模态接口响应片段（便于核对接入点/model）：%s",
                                snippet[:800],
                            )
                    time.sleep(delay)
                    continue
                data = r.json()
                raw_emb = _extract_embedding_from_json(data)
            out, norm_err = _normalize_and_validate_embedding(
                raw_emb,
                dims,
                model,
                base,
                path,
            )
            if out is not None:
                if debug_api_io_enabled():
                    log_flow(
                        "embedding_call",
                        {
                            "base": base[:96],
                            "path": path,
                            "model": model,
                            "modality": modality,
                            "ok": True,
                            "dim": len(out),
                        },
                    )
                return out
            last_err = last_err or norm_err
            time.sleep(delay)
            continue
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as e:
            last_err = str(e)
        time.sleep(delay)
    logger.warning("embedding failed: %s", last_err)
    if debug_api_io_enabled():
        log_flow(
            "embedding_call",
            {
                "base": base[:96],
                "path": path,
                "model": model,
                "modality": modality,
                "ok": False,
                "error": (last_err or "")[:400],
            },
        )
    return None


def embed_image_url(url: str) -> Optional[List[float]]:
    if not url:
        return None
    cache_key = "img:" + hashlib.md5(url.encode()).hexdigest()
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    result = _embed_multimodal_parts(
        [{"type": "image_url", "image_url": {"url": url}}],
        modality="image",
    )
    if result:
        _cache_put(cache_key, result)
    return result


def embed_text(text: str) -> Optional[List[float]]:
    """纯文本向量（商品 search_text / 查询关键词，与图文向量索引分离）。"""
    chunk = (text or "").strip()
    if not chunk:
        return None
    truncated = chunk[:2000]
    cache_key = "txt:" + hashlib.md5(truncated.encode()).hexdigest()
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    result = _embed_multimodal_parts(
        [{"type": "text", "text": truncated}],
        modality="text",
    )
    if result:
        _cache_put(cache_key, result)
    return result


def embed_image_base64(
    b64: str,
    mime: str = "image/jpeg",
) -> Optional[List[float]]:
    if not b64:
        return None
    # 小图走 data URI，避免临时 URL
    raw = base64.b64decode(b64, validate=False)
    if len(raw) > 8 * 1024 * 1024:
        logger.warning("image too large for embedding")
        return None
    data_uri = f"data:{mime};base64,{b64}"
    return embed_image_url(data_uri)
