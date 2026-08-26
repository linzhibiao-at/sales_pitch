"""私有部署裸 vLLM（partner）搭配美学打分 + 推荐理由。

调用方式参考 ``scripts/partner_call_example.py``：逐套多图 + V6 system prompt，
调 ``/v1/chat/completions``，解析模型输出的五维 + 综合评分 + 评语。
综合评分(0-10) 归一化为 0-1 用于排序，评语作为推荐理由。

与 ``outfit_ranker.llm_rank_outfits`` 并列：本模块负责 ``llm_rank_reason_method=partner``
时的打分+理由，逐套并行调用（打分与理由在一次调用中同时产出），完成后按综合评分排序返回。
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

import httpx

from backend.api_debug import debug_api_io_enabled, log_flow
from backend.config import env_or_empty, load_config
from backend.models import normalize_gender_first, normalize_season

logger = logging.getLogger(__name__)

# 复用 scripts/partner_call_example.py 中的 V6 prompt 与解析逻辑（单一事实源），
# 仅 base_url/model/timeout 等改为 config 驱动。
from scripts.partner_call_example import (  # noqa: PLC0415
    SCORE_KEYS,
    build_messages,
    extract_scores,
)
# 复用 scripts/v8sws_client.py 中的 V8（fila-outfit-v8sws）prompt/解析/外部 f（单一事实源）。
# V8 与 V6 差异：system prompt 不同、输出顺序（综合评分在五维之后）、综合分用外部 f
# 覆写（apply_f，非模型 raw）、max_tokens 700。
from scripts.v8sws_client import (  # noqa: PLC0415
    MAX_TOKENS as V8_MAX_TOKENS,
    SYSTEM_PROMPT_V8,
    build_user_text as build_user_text_v8,
    extract_scores as extract_scores_v8,
)


def _is_v8_model(model: Optional[str]) -> bool:
    """是否为 fila-outfit-v8sws（按模型名判定，走 V8 调用路径）。"""
    return bool(model) and "v8sws" in model.lower()


def _resolve_model(pcfg: dict, model_override: Optional[str]) -> str:
    """partner 模型名始终取 config（req.llm_model 不可转发，会 404）。"""
    return model_override or pcfg.get("model") or "fila-outfit-v6_1"


def _build_messages_v8(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """构造 V8 messages：多图 image_url + V8 user_text（逐字对齐 v8sws_client）。"""
    user_content = [
        {"type": "image_url", "image_url": {"url": it["image_url"]}}
        for it in items if it.get("image_url")
    ]
    user_content.append({"type": "text", "text": build_user_text_v8(items)})
    return [
        {"role": "system", "content": SYSTEM_PROMPT_V8},
        {"role": "user", "content": user_content},
    ]

# ---------- shared httpx client (connection pool reuse) ----------
_partner_http_pool: httpx.Client | None = None
_partner_http_lock = __import__("threading").Lock()


def _get_partner_http_client(timeout: float) -> httpx.Client:
    """Return a process-wide httpx.Client dedicated to partner vLLM calls."""
    global _partner_http_pool
    with _partner_http_lock:
        if _partner_http_pool is None or _partner_http_pool.is_closed:
            _partner_http_pool = httpx.Client(timeout=timeout)
        return _partner_http_pool


def _partner_config(cfg: Optional[dict] = None) -> dict:
    """Read ``recommend.partner_rank_reason`` from config."""
    cfg = cfg or load_config()
    return (cfg.get("recommend") or {}).get("partner_rank_reason") or {}


def _partner_max_workers(cfg: Optional[dict] = None) -> int:
    cfg = cfg or load_config()
    return int((_partner_config(cfg)).get("max_workers") or 5)


def _season_str(raw: object) -> str:
    s = normalize_season(raw)
    return "/".join(s) if s else ""


def _partner_build_items(outfit: Dict[str, Any]) -> list[dict[str, Any]]:
    """将 outfit items 映射为 partner_call_example.make_item_desc 所需字段。

    字段对齐：display_image→image_url、category_l2→cat_alias、role→up_down、
    gender→sex；season 归一化后拼成字符串（example 期望字符串）。
    vLLM 单次最多 5 张图，超出截断。
    """
    items: list[dict[str, Any]] = []
    for it in outfit.get("items") or []:
        img = it.get("display_image") or ""
        if not img:
            continue
        items.append({
            "sku_id": it.get("sku_id") or "",
            "image_url": img,
            "title": it.get("title") or "",
            "series": it.get("series") or "",
            "cat_alias": it.get("category_l2") or it.get("category_l1") or "",
            "up_down": it.get("role") or "",
            "sex": normalize_gender_first(it.get("gender")) or "",
            "season": _season_str(it.get("season")),
            "color_name": it.get("color_name") or "",
            "price": it.get("price") or 0,
        })
        if len(items) >= 5:
            break
    return items


def _partner_call(
    items: list[dict[str, Any]],
    pcfg: dict,
    *,
    model_override: Optional[str],
) -> str:
    """调用裸 vLLM /v1/chat/completions，返回模型输出文本（失败返回空串）。"""
    model = _resolve_model(pcfg, model_override)
    is_v8 = _is_v8_model(model)
    # 两版部署实例不同，URL 在 config 显式配置：V8 用 base_url_v8，V6 用 base_url。
    # base_url_v8 未配时回退 base_url，保证只改 model 也能切版。
    base = (pcfg.get("base_url_v8") or pcfg.get("base_url") or "").rstrip("/") if is_v8 \
        else (pcfg.get("base_url") or "").rstrip("/")
    if not base:
        logger.warning("partner_ranker: base_url 未配置，跳过")
        return ""
    timeout = float(pcfg.get("timeout_sec") or 60)
    if is_v8:
        max_tokens = int(pcfg.get("max_tokens_v8") or V8_MAX_TOKENS)
        messages = _build_messages_v8(items)
    else:
        max_tokens = int(pcfg.get("max_tokens") or 600)
        messages = build_messages(items)
    temperature = float(pcfg.get("temperature") or 0)
    key_env = pcfg.get("api_key_env") or ""
    api_key = env_or_empty(key_env) if key_env else ""

    url = f"{base}/v1/chat/completions"
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    if debug_api_io_enabled():
        host = base.split("//", 1)[-1].split("/", 1)[0] if "//" in base else base[:80]
        log_flow(
            "partner_call_in",
            {"model": model, "base_host": host, "item_count": len(items)},
        )

    retries = 2
    retry_delay = 2.0
    last_err: str | None = None
    for attempt in range(retries + 1):
        try:
            client = _get_partner_http_client(timeout)
            r = client.post(url, headers=headers, json=body)
            if r.status_code >= 400:
                last_err = f"HTTP {r.status_code}: {(r.text or '')[:500]}"
                if r.status_code != 429 and r.status_code < 500:
                    logger.warning("partner_ranker 不可重试错误 %d: %s",
                                   r.status_code, last_err)
                    return ""
                logger.warning("partner_ranker 第%d次请求失败 %s，%.1fs 后重试",
                               attempt + 1, last_err, retry_delay)
                time.sleep(retry_delay)
                continue
            data = r.json()
            raw = (
                (data.get("choices") or [{}])[0]
                .get("message", {})
                .get("content", "")
                or ""
            )
            if debug_api_io_enabled():
                log_flow(
                    "partner_call_out",
                    {"content_len": len(raw), "content_preview": raw[:600] if raw else ""},
                )
            return raw
        except httpx.TimeoutException as e:
            last_err = f"timeout: {e}"
            logger.warning("partner_ranker 第%d次请求超时，%.1fs 后重试",
                           attempt + 1, retry_delay)
        except httpx.HTTPError as e:
            last_err = f"http error: {e}"
            logger.warning("partner_ranker 第%d次请求网络错误: %s，%.1fs 后重试",
                           attempt + 1, e, retry_delay)
        time.sleep(retry_delay)
    logger.error("partner_ranker 重试 %d 次后仍失败: %s", retries + 1, last_err)
    return ""


def _partner_score_one(
    outfit: Dict[str, Any],
    pcfg: dict,
    *,
    model_override: Optional[str],
) -> Tuple[str, dict[str, Any]]:
    """对单套搭配调用 partner vLLM，返回 (outfit_id, {score, brief, reason, parse_ok})。

    综合评分(0-10) 归一化为 0-1；评语作为 reason；brief 拼接五维供调试台展示。
    """
    oid = str(outfit.get("outfit_id") or "")
    items = _partner_build_items(outfit)
    if len(items) < 2:
        logger.warning("partner_score_one[%s] 可用带图单品不足 2 件，回退 0.5", oid)
        return oid, {"score": 0.5, "brief": "", "reason": "", "parse_ok": False}

    text = _partner_call(items, pcfg, model_override=model_override)
    if not text:
        return oid, {"score": 0.5, "brief": "", "reason": "", "parse_ok": False}

    is_v8 = _is_v8_model(_resolve_model(pcfg, model_override))
    scores = extract_scores_v8(text) if is_v8 else extract_scores(text)
    composite = scores.get("composite")
    if composite is None:
        logger.warning("partner_score_one[%s] 解析综合评分失败: %s",
                       oid, (text or "")[:200])
        return oid, {"score": 0.5, "brief": "", "reason": scores.get("comment") or "",
                     "parse_ok": False}

    norm = max(0.0, min(1.0, float(composite) / 10.0))
    dim_str = " ".join(
        f"{k}:{scores.get(k)}" for k in ("color", "style", "fashion", "material", "layering")
        if scores.get(k) is not None
    )
    brief = f"综合{composite}/10 " + dim_str
    return oid, {
        "score": norm,
        "brief": brief,
        "reason": scores.get("comment") or "",
        "parse_ok": bool(scores.get("parse_ok")),
    }


def partner_rank_outfits(
    outfits: List[Dict[str, Any]],
    *,
    cfg: Optional[dict] = None,
    model_override: Optional[str] = None,
) -> List[Tuple[float, Dict[str, Any]]]:
    """使用私有部署 vLLM 对搭配逐套并行打分+生成理由，按综合评分降序返回。

    与 ``llm_rank_outfits`` 返回结构一致：(score, outfit) 列表，并将
    ``_llm_score`` / ``_llm_brief`` / ``_llm_reason`` 写入 outfit，供下游
    breakdown 与 reason 字段复用（enable_llm_rank_reason 流程）。
    """
    if not outfits:
        return []

    cfg = cfg or load_config()
    pcfg = _partner_config(cfg)
    max_workers = _partner_max_workers(cfg)
    rec_cfg = cfg.get("recommend") or {}
    use_template_reason = rec_cfg.get("reason_generation_mode") == "template"

    scores_map: dict[str, dict[str, Any]] = {}
    workers = min(max_workers, len(outfits))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_partner_score_one, o, pcfg, model_override=model_override): o
            for o in outfits
        }
        for fut in as_completed(futures):
            try:
                oid, info = fut.result()
                scores_map[oid] = info
            except Exception:
                logger.exception("partner_rank_outfits: single outfit scoring failed")
                o = futures[fut]
                fallback_oid = str(o.get("outfit_id") or "")
                if fallback_oid:
                    scores_map[fallback_oid] = {"score": 0.5, "brief": "", "reason": "", "parse_ok": False}

    ranked: List[Tuple[float, Dict[str, Any]]] = []
    for o in outfits:
        oid = str(o.get("outfit_id") or "")
        info = scores_map.get(oid, {"score": 0.5, "brief": "", "reason": ""})
        score = float(info["score"])
        o["_llm_score"] = score
        o["_llm_brief"] = info.get("brief") or ""
        if use_template_reason:
            from backend.dphs_reason_store import build_template_reason  # noqa: PLC0415
            o["_llm_reason"] = build_template_reason(o)
        else:
            o["_llm_reason"] = info.get("reason") or ""
        ranked.append((score, o))

    ranked.sort(key=lambda x: -x[0])
    return ranked


# 暴露给 outfit_ranker 统一导入面
__all__ = ["partner_rank_outfits"]
