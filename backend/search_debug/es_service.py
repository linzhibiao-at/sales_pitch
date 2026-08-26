"""ES 文本检索调试服务：直接文本检索 vs LLM 意图解析检索对比。

适配 fila_agent_html 的 elasticsearch + models.intent_llm 配置。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from backend.config import (
    create_elasticsearch_client,
    get_elasticsearch_hosts,
    get_elasticsearch_index,
    load_config,
)

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]  # fila_agent_html/


def _prompt_path() -> Path:
    return _ROOT / "prompt" / "sku_search_intent.md"


def _load_intent_prompt() -> str:
    path = _prompt_path()
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return "你是检索意图解析器。只输出 JSON。"


def _extract_json_object(text: str) -> dict[str, Any]:
    if not text:
        return {}
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return {}
    try:
        out = json.loads(m.group(0))
        return out if isinstance(out, dict) else {}
    except json.JSONDecodeError:
        return {}


# ── ES 查询构建 ──

def _search_query(q: str) -> dict[str, Any]:
    text = (q or "").strip()
    if not text:
        return {"match_all": {}}
    fields = [
        "title^2",
        "name^2",
        "search_text",
        "search_keywords",
        "sku_id",
        "spu_id",
        "outfit_id",
        "master_sku_id",
    ]
    return {
        "multi_match": {
            "query": text,
            "fields": fields,
            "type": "best_fields",
            "operator": "or",
            "lenient": True,
            "fuzziness": "AUTO",
        },
    }


def _pick_sku(source: dict[str, Any]) -> str:
    sid = source.get("sku_id")
    if sid:
        return str(sid)
    mid = source.get("master_sku_id")
    if mid:
        return str(mid)
    items = source.get("items")
    if isinstance(items, list) and items:
        first = items[0]
        if isinstance(first, dict) and first.get("sku_id"):
            return str(first["sku_id"])
    return ""


def _pick_image(source: dict[str, Any]) -> str:
    for key in ("display_image", "index_image", "tryon_image"):
        val = source.get(key)
        if val:
            return str(val)
    return ""


def _pick_title(source: dict[str, Any]) -> str:
    for key in ("title", "name"):
        val = source.get(key)
        if val:
            return str(val)
    return ""


def _run_search_with_query(
    cfg: dict[str, Any],
    index: str,
    query: dict[str, Any],
    size: int,
) -> dict[str, Any]:
    index = (index or "").strip()
    if not index:
        raise ValueError("索引名为空")
    hosts = get_elasticsearch_hosts(cfg)
    es_block = cfg.get("elasticsearch") or {}
    user_env = str(es_block.get("username_env") or "")
    pwd_env = str(es_block.get("password_env") or "")
    username = os.environ.get(user_env, "") if user_env else ""
    password = os.environ.get(pwd_env, "") if pwd_env else ""
    timeout = int(es_block.get("request_timeout_sec") or 30)
    client = create_elasticsearch_client(
        hosts,
        username=username,
        password=password,
        timeout_sec=timeout,
    )
    try:
        resp = client.search(
            index=index,
            size=size,
            query=query,
            _source=True,
            request_timeout=timeout,
        )
    finally:
        client.close()
    hits_out: list[dict[str, Any]] = []
    for h in resp.get("hits", {}).get("hits", []) or []:
        src = h.get("_source") or {}
        if not isinstance(src, dict):
            src = {}
        sku = _pick_sku(src)
        hits_out.append({
            "id": str(h.get("_id") or ""),
            "score": h.get("_score"),
            "sku_id": sku,
            "title": _pick_title(src),
            "image_url": _pick_image(src),
            "source": src,
        })
    total = resp.get("hits", {}).get("total")
    if isinstance(total, dict):
        total_val = total.get("value")
    else:
        total_val = total
    return {
        "index": index,
        "took_ms": resp.get("took"),
        "total": total_val,
        "hits": hits_out,
    }


def _run_search(cfg: dict[str, Any], index: str, q: str, size: int) -> dict[str, Any]:
    return _run_search_with_query(cfg, index, _search_query(q), size)


# ── LLM 意图解析 ──

def _user_message_with_index(user_text: str, index_name: str) -> str:
    text = (user_text or "").strip()[:4000]
    index = (index_name or "").strip()
    return f"目标索引：{index}\n检索词：{text}"


def call_llm_extract(
    user_text: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
    index_name: str = "",
    timeout_sec: float = 60.0,
) -> tuple[dict[str, Any], str, Optional[str]]:
    """调用 OpenAI 兼容 Chat Completions 解析检索意图。

    返回 (parsed_dict, raw_assistant_text, error_message)。
    """
    import urllib.error
    import urllib.request

    text = (user_text or "").strip()[:4000]
    if not text:
        return {}, "", None
    url = base_url.rstrip("/") + "/chat/completions"
    system = _load_intent_prompt()
    user_content = _user_message_with_index(text, index_name)
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.1,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:2000]
        logger.warning("LLM HTTPError %s: %s", e.code, err_body)
        return {}, "", f"HTTP {e.code}: {err_body}"
    except urllib.error.URLError as e:
        logger.warning("LLM URLError: %s", e)
        return {}, "", str(e.reason or e)
    except TimeoutError:
        return {}, "", "请求超时"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}, "", "响应非 JSON"
    choices = payload.get("choices") or []
    if not choices:
        return {}, "", "接口未返回 choices"
    msg = choices[0].get("message") or {}
    content = str(msg.get("content") or "").strip()
    parsed = _extract_json_object(content)
    return parsed, content, None


# ── ES query 组装 ──

def _unwrap_es_query(parsed: dict[str, Any]) -> dict[str, Any]:
    if not parsed:
        return {}
    inner = parsed.get("query")
    if isinstance(inner, dict):
        return inner
    if any(
        key in parsed
        for key in ("bool", "multi_match", "match_all", "term", "match")
    ):
        return parsed
    return {}


def _strip_category_l2(node: Any) -> Any:
    """递归移除 query 中的 category_l2 条件。"""
    if isinstance(node, dict):
        term = node.get("term")
        if isinstance(term, dict) and "category_l2" in term:
            return None
        out: dict[str, Any] = {}
        for key, val in node.items():
            cleaned = _strip_category_l2(val)
            if cleaned is None:
                continue
            if isinstance(cleaned, list):
                cleaned = [x for x in cleaned if x is not None]
                if key in ("filter", "must", "should", "must_not") and not cleaned:
                    continue
            out[key] = cleaned
        return out
    if isinstance(node, list):
        items = [_strip_category_l2(x) for x in node]
        return [x for x in items if x is not None]
    return node


def _text_for_must(extraction: dict[str, Any], fallback_q: str) -> str:
    from backend.search_debug.search_slots import text_for_must
    return text_for_must(extraction, fallback_q)


def _sku_text_fields() -> list[str]:
    return ["title^2", "search_text", "search_keywords", "sku_id", "spu_id"]


def _outfit_text_fields() -> list[str]:
    return ["name^2", "search_text", "outfit_id", "master_sku_id", "master_spu_id"]


def _multi_must(text: str, fields: list[str]) -> dict[str, Any]:
    if not text:
        return {"match_all": {}}
    return {
        "multi_match": {
            "query": text,
            "fields": fields,
            "type": "best_fields",
            "operator": "or",
            "lenient": True,
            "fuzziness": "AUTO",
        },
    }


def _append_term(filters: list[dict[str, Any]], field: str, value: object) -> None:
    if value is None:
        return
    s = str(value).strip()
    if not s:
        return
    filters.append({"term": {field: s}})


def _append_season_sku(filters: list[dict[str, Any]], seasons: object) -> None:
    if not isinstance(seasons, list) or not seasons:
        return
    should: list[dict[str, Any]] = []
    for s in seasons:
        token = str(s).strip()
        if not token:
            continue
        should.append({"wildcard": {"season": f"*{token}*"}})
    if not should:
        return
    if len(should) == 1:
        filters.append(should[0])
    else:
        filters.append({"bool": {"should": should, "minimum_should_match": 1}})


def _looks_like_legacy_extraction(parsed: dict[str, Any]) -> bool:
    legacy_keys = (
        "gender", "category_l1", "keywords", "role", "season", "price_max",
    )
    return any(k in parsed for k in legacy_keys)


def build_sku_query(extraction: dict[str, Any], fallback_q: str) -> dict[str, Any]:
    filters: list[dict[str, Any]] = []
    _append_term(filters, "gender", extraction.get("gender"))
    _append_term(filters, "category_l1", extraction.get("category_l1"))
    _append_term(filters, "role", extraction.get("role"))
    _append_term(filters, "series", extraction.get("series"))
    _append_term(filters, "sku_id", extraction.get("sku_id"))
    _append_term(filters, "spu_id", extraction.get("spu_id"))
    _append_term(filters, "group_brand", extraction.get("group_brand"))
    _append_season_sku(filters, extraction.get("season"))
    pm = extraction.get("price_max")
    if pm is not None:
        try:
            v = float(pm)
            filters.append({"range": {"price": {"lte": v}}})
        except (TypeError, ValueError):
            pass
    must = _multi_must(_text_for_must(extraction, fallback_q), _sku_text_fields())
    if not filters:
        return must
    return {"bool": {"must": [must], "filter": filters}}


def build_outfit_query(extraction: dict[str, Any], fallback_q: str) -> dict[str, Any]:
    filters: list[dict[str, Any]] = []
    _append_term(filters, "gender", extraction.get("gender"))
    role = extraction.get("role")
    if role is not None and str(role).strip():
        filters.append({"term": {"roles": str(role).strip()}})
    _append_term(filters, "master_sku_id", extraction.get("sku_id"))
    _append_term(filters, "master_spu_id", extraction.get("spu_id"))
    pm = extraction.get("price_max")
    if pm is not None:
        try:
            v = float(pm)
            filters.append({"range": {"price_total": {"lte": v}}})
        except (TypeError, ValueError):
            pass
    must = _multi_must(_text_for_must(extraction, fallback_q), _outfit_text_fields())
    if not filters:
        return must
    return {"bool": {"must": [must], "filter": filters}}


def _index_looks_outfit(index_name: str) -> bool:
    n = (index_name or "").lower()
    return "outfit" in n


def build_query_for_index(index_name: str, extraction: dict[str, Any],
                          fallback_q: str) -> dict[str, Any]:
    if _index_looks_outfit(index_name):
        return build_outfit_query(extraction, fallback_q)
    return build_sku_query(extraction, fallback_q)


def fallback_simple_query(q: str) -> dict[str, Any]:
    return _search_query(q)


def coerce_llm_es_query(
    parsed: dict[str, Any],
    fallback_q: str,
    *,
    index_name: str = "",
) -> dict[str, Any]:
    query = _unwrap_es_query(parsed)
    if not query:
        if _looks_like_legacy_extraction(parsed):
            return build_query_for_index(index_name, parsed, fallback_q)
        return fallback_simple_query(fallback_q)
    cleaned = _strip_category_l2(query)
    if isinstance(cleaned, dict) and cleaned:
        return cleaned
    return fallback_simple_query(fallback_q)


# ── 公开 API ──

def get_es_config() -> dict[str, Any]:
    """获取 ES 检索调试的配置信息。"""
    cfg = load_config()
    hosts = get_elasticsearch_hosts(cfg)
    from_env = bool((os.environ.get("ES_HOSTS") or "").strip())
    llm = cfg.get("models", {}).get("intent_llm") or {}
    es_indices = (cfg.get("elasticsearch") or {}).get("indices") or {}
    return {
        "index_default": str(es_indices.get("skus") or ""),
        "default_size": 20,
        "elasticsearch_hosts": hosts,
        "elasticsearch_hosts_from_es_hosts_env": from_env,
        "llm_enabled": bool(llm.get("base_url")),
        "llm_model": str(llm.get("model") or ""),
    }


def _to_column(index: str, raw: dict[str, Any] | BaseException) -> dict[str, Any]:
    if isinstance(raw, BaseException):
        logger.error("ES 检索失败 index=%s err=%s", index, raw)
        return {"ok": False, "index": index, "error": str(raw), "hits": []}
    return {
        "ok": True,
        "index": str(raw.get("index") or index),
        "took_ms": raw.get("took_ms"),
        "total": raw.get("total"),
        "hits": list(raw.get("hits") or []),
    }


def search_es_direct(q: str, index: str = "", size: int = 20) -> dict[str, Any]:
    """直接文本检索（单次）。"""
    cfg = load_config()
    es_indices = (cfg.get("elasticsearch") or {}).get("indices") or {}
    index_name = (index or "").strip() or str(es_indices.get("skus") or "")
    if not index_name:
        raise ValueError("请填写索引名")
    result = _run_search(cfg, index_name, q, size)
    col = _to_column(index_name, result)
    return {"query": q, "left": col, "right": col}


async def search_es_smart(q: str, index: str = "", size: int = 20) -> dict[str, Any]:
    """智能检索对比：左列直接文本，右列 LLM 意图解析。"""
    cfg = load_config()
    es_indices = (cfg.get("elasticsearch") or {}).get("indices") or {}
    index_name = (index or "").strip() or str(es_indices.get("skus") or "")
    if not index_name:
        raise ValueError("请填写索引名")

    # LLM 意图解析
    llm = cfg.get("models", {}).get("intent_llm") or {}
    base_url = str(llm.get("base_url") or "").strip()
    model = str(llm.get("model") or "qwen3.5-flash").strip()
    api_key_env = str(llm.get("api_key_env") or "ANTA_LLM_API_KEY").strip()
    timeout_sec = float(llm.get("timeout_sec") or 60)
    api_key = os.environ.get(api_key_env, "") if api_key_env else ""

    extraction: dict[str, Any] = {}
    llm_error: Optional[str] = None
    llm_raw: Optional[str] = None
    llm_called = False
    llm_elapsed_ms: Optional[float] = None

    if base_url and api_key:
        def _call() -> tuple[dict[str, Any], str, Optional[str]]:
            return call_llm_extract(
                q, base_url=base_url, api_key=api_key, model=model,
                index_name=index_name, timeout_sec=timeout_sec,
            )

        t0 = time.perf_counter()
        extraction, raw_text, err = await asyncio.to_thread(_call)
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        llm_called = True
        llm_elapsed_ms = elapsed_ms
        llm_error = err
        llm_raw = (raw_text or "")[:4000] if raw_text else None
        if llm_error is None and not extraction and not (q or "").strip():
            llm_error = "检索词为空"
    else:
        llm_error = "LLM 未启用（未配置 base_url 或 api_key）"

    fb_q = q
    err_msg = llm_error or ""
    use_fallback = (
        ("未启用" in err_msg)
        or ("未配置" in err_msg)
        or ("未设置环境变量" in err_msg)
        or ("HTTP " in err_msg)
        or ("超时" in err_msg)
        or ("choices" in err_msg)
        or ("非 JSON" in err_msg)
        or (bool(err_msg) and not extraction and llm_called)
    )
    q_direct = fallback_simple_query(fb_q)
    if use_fallback:
        q_parsed = fallback_simple_query(fb_q)
    else:
        q_parsed = coerce_llm_es_query(extraction, fb_q, index_name=index_name)

    async def _one(qobj: dict[str, Any]) -> dict[str, Any] | BaseException:
        try:
            return await asyncio.to_thread(
                _run_search_with_query, cfg, index_name, qobj, size,
            )
        except BaseException as exc:
            return exc

    raw_left, raw_right = await asyncio.gather(_one(q_direct), _one(q_parsed))

    return {
        "query": q,
        "index": index_name,
        "extraction": extraction,
        "es_query_left": q_direct,
        "es_query_right": q_parsed,
        "llm_enabled": llm_called,
        "llm_elapsed_ms": llm_elapsed_ms,
        "llm_error": llm_error,
        "llm_raw_excerpt": llm_raw,
        "left": _to_column(index_name, raw_left),
        "right": _to_column(index_name, raw_right),
    }