"""虚拟试穿服务：图片拼接 + vertex-tryon API 调用。

参考 scripts/test_tryon.py --mode single 实现。
将搭配中 top/bottoms/shoes 的 tryon_image 横向拼接为一张图，
再调用 vertex-tryon 模型获取穿搭效果图。

优化策略：
  1. 先全部提交再统一轮询（fire-and-gather），避免 submit→poll 串行等待
  2. 跨 outfit 并行下载图片 + 同 URL 内存缓存
  3. max_workers 可通过 config.yaml recommend.tryon.max_workers 配置
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import httpx

from backend.config import env_or_empty

logger = logging.getLogger(__name__)

# ── 图片下载缓存（同 URL 只下载一次） ─────────────────────

_image_cache: dict[str, bytes] = {}
_image_cache_lock = threading.Lock()


def _download_image_bytes(url: str, *, timeout: float = 30.0) -> bytes:
    """下载图片 URL，返回原始字节。"""
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url, headers={"Referer": url})
        resp.raise_for_status()
        return resp.content


def _download_image_bytes_cached(url: str, *, timeout: float = 30.0) -> bytes:
    """带缓存的图片下载，同一 URL 只下载一次。"""
    with _image_cache_lock:
        if url in _image_cache:
            return _image_cache[url]
    raw = _download_image_bytes(url, timeout=timeout)
    with _image_cache_lock:
        _image_cache[url] = raw
    return raw


# ── 图片拼接 ──────────────────────────────────────────────


def _stitch_images_to_base64(
    image_sources: list[str],
    *,
    timeout: float = 30.0,
    download_workers: int = 8,
) -> str:
    """将多张图片横向拼接，返回 JPEG base64 字符串（不含 data: 前缀）。

    image_sources 中每项可以是：
      - URL（http/https 开头）→ 并行下载（带缓存）
      - 纯 base64 字符串 → 直接解码
    """
    from PIL import Image  # noqa: PLC0415

    n = len(image_sources)
    images: list[Image.Image | None] = [None] * n
    url_tasks: list[tuple[int, str]] = []

    for i, src in enumerate(image_sources):
        if src.startswith(("http://", "https://")):
            url_tasks.append((i, src))
        elif src.startswith("data:"):
            raw = base64.b64decode(src.split(",", 1)[1])
            images[i] = Image.open(io.BytesIO(raw)).convert("RGB")
        else:
            raw = base64.b64decode(src)
            images[i] = Image.open(io.BytesIO(raw)).convert("RGB")

    if url_tasks:
        workers = min(download_workers, len(url_tasks))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_download_image_bytes_cached, url, timeout=timeout): idx
                for idx, url in url_tasks
            }
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    raw = fut.result()
                    images[idx] = Image.open(io.BytesIO(raw)).convert("RGB")
                except Exception as exc:
                    logger.warning(
                        "tryon: 下载图片失败 idx=%d url=%s: %s",
                        idx, image_sources[idx][:120], exc,
                    )

    valid = [img for img in images if img is not None]
    if not valid:
        return ""

    target_h = max(img.height for img in valid)
    resized: list[Image.Image] = []
    for img in valid:
        if img.height != target_h:
            ratio = target_h / img.height
            new_w = int(img.width * ratio)
            img = img.resize((new_w, target_h), Image.LANCZOS)
        resized.append(img)

    total_w = sum(img.width for img in resized)
    canvas = Image.new("RGB", (total_w, target_h), (255, 255, 255))
    x = 0
    for img in resized:
        canvas.paste(img, (x, 0))
        x += img.width

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ── API 响应解析（与 test_tryon.py 保持一致） ──────────────


def _unwrap_api_data(resp: dict[str, Any]) -> dict[str, Any]:
    """兼容 {code, data, msg} 与扁平响应。"""
    inner = resp.get("data")
    if isinstance(inner, dict):
        return inner
    return resp


def _extract_prediction_id(resp: dict[str, Any]) -> str:
    body = _unwrap_api_data(resp)
    return str(
        body.get("predictionId")
        or body.get("prediction_id")
        or resp.get("predictionId")
        or resp.get("prediction_id")
        or ""
    )


def _extract_status(resp: dict[str, Any]) -> str:
    body = _unwrap_api_data(resp)
    return str(body.get("status") or resp.get("status") or "")


def _extract_result_image(data: dict[str, Any]) -> str:
    data = _unwrap_api_data(data)
    output = data.get("output")
    if isinstance(output, str) and output.strip():
        return output.strip()
    if isinstance(output, list):
        for item in output:
            if isinstance(item, str) and item.strip():
                return item.strip()
            if isinstance(item, dict):
                for key in ("image", "image_url", "url", "result"):
                    val = item.get(key)
                    if val and isinstance(val, str):
                        return val
    if isinstance(output, dict):
        for key in ("image", "image_url", "result", "output_image"):
            val = output.get(key)
            if val and isinstance(val, str):
                return val
    for pred in data.get("predictions") or []:
        if not isinstance(pred, dict):
            continue
        for key in ("bytesBase64Encoded", "image", "image_url"):
            val = pred.get(key)
            if val and isinstance(val, str):
                return val
    return ""


# ── Try-on API 调用 ──────────────────────────────────────


def _tryon_submit(
    *,
    base_url: str,
    token: str,
    person_image: str,
    product_image: str,
    model_id: str = "vertex-tryon",
    timeout: float = 180.0,
) -> str:
    """提交异步试穿任务，返回 predictionId。"""
    url = f"{base_url.rstrip('/')}/prediction/async_create"
    body = {
        "input": {
            "model_id": model_id,
            "person_image": person_image,
            "product_image": product_image,
            "sample_count": 1,
        },
    }
    headers = {"token": token, "Content-Type": "application/json"}
    logger.info(
        "试穿提交 person=%s product=(base64 len=%d)",
        person_image[:80],
        len(product_image),
    )
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
    pid = _extract_prediction_id(data)
    if not pid:
        raise RuntimeError(f"async_create 未返回 predictionId: {data}")
    logger.info("试穿 predictionId=%s", pid)
    return pid


def _tryon_poll(
    *,
    base_url: str,
    token: str,
    prediction_id: str,
    poll_interval: float = 5.0,
    max_attempts: int = 60,
    timeout: float = 180.0,
) -> dict[str, Any]:
    """轮询试穿结果直到成功/失败/超时。"""
    url = f"{base_url.rstrip('/')}/prediction/query"
    headers = {"token": token}
    for attempt in range(max_attempts):
        if attempt > 0:
            time.sleep(poll_interval)
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(
                url,
                params={"predictionId": prediction_id},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
        status = _extract_status(data).lower()
        logger.info("试穿轮询 #%d status=%s pid=%s", attempt + 1, status, prediction_id)
        if status == "succeeded":
            return _unwrap_api_data(data) if data.get("data") else data
        if status in ("failed", "error", "cancelled"):
            raise RuntimeError(
                f"试穿失败 pid={prediction_id} status={status} "
                f"body={json.dumps(data, ensure_ascii=False)[:500]}"
            )
    raise TimeoutError(
        f"试穿超时 pid={prediction_id} "
        f"attempts={max_attempts} interval={poll_interval}s"
    )


def _prepare_outfit_product_image(
    outfit_card: dict[str, Any],
    oid: str,
) -> tuple[str | None, str]:
    """收集并拼接单套搭配的商品图，返回 (base64, 错误原因)。

    错误原因非空时 base64 为 None。
    """
    items = outfit_card.get("items") or []
    image_sources: list[str] = []
    roles_found: list[str] = []
    items_missing: list[str] = []
    for it in items:
        role = it.get("role") or "unknown"
        url = (it.get("tryon_image") or "").strip()
        if url:
            image_sources.append(url)
            roles_found.append(role)
        else:
            items_missing.append(f"{role}(tryon_image为空)")

    if not image_sources:
        reason = f"无可用tryon_image, items缺失: {items_missing}"
        logger.info("tryon: outfit %s %s", oid, reason)
        return None, reason

    logger.info(
        "tryon: outfit %s 准备拼接, roles=%s, missing=%s, image_count=%d",
        oid, roles_found, items_missing, len(image_sources),
    )

    b64 = _stitch_images_to_base64(image_sources, timeout=30.0)
    if not b64:
        reason = "图片拼接失败(结果为空)"
        logger.warning("tryon: outfit %s %s", oid, reason)
        return None, reason
    return b64, ""


def _tryon_poll_batch(
    *,
    base_url: str,
    token: str,
    pending: dict[str, str],
    poll_interval: float = 5.0,
    max_attempts: int = 60,
    timeout: float = 180.0,
) -> dict[str, dict[str, Any]]:
    """批量轮询多个试穿任务，统一返回 {prediction_id: result_dict}。

    pending: {prediction_id: outfit_id}
    """
    url = f"{base_url.rstrip('/')}/prediction/query"
    headers = {"token": token}
    results: dict[str, dict[str, Any]] = {}
    remaining = dict(pending)

    for attempt in range(max_attempts):
        if not remaining:
            break
        if attempt > 0:
            time.sleep(poll_interval)

        still_pending: dict[str, str] = {}
        for pid, oid in remaining.items():
            try:
                with httpx.Client(timeout=timeout) as client:
                    resp = client.get(
                        url,
                        params={"predictionId": pid},
                        headers=headers,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                status = _extract_status(data).lower()
                logger.info(
                    "试穿轮询 #%d status=%s pid=%s oid=%s",
                    attempt + 1, status, pid, oid,
                )
                if status == "succeeded":
                    body = _unwrap_api_data(data) if data.get("data") else data
                    results[pid] = body
                elif status in ("failed", "error", "cancelled"):
                    results[pid] = {
                        "_error": True,
                        "status": status,
                        "body": data,
                    }
                else:
                    still_pending[pid] = oid
            except Exception as exc:
                logger.warning(
                    "试穿轮询异常 pid=%s oid=%s: %s", pid, oid, exc,
                )
                still_pending[pid] = oid

        remaining = still_pending

    for pid, oid in remaining.items():
        results[pid] = {
            "_error": True,
            "status": "timeout",
            "body": {"message": f"轮询超时 attempts={max_attempts}"},
        }
        logger.warning("试穿超时 pid=%s oid=%s", pid, oid)

    return results


def tryon_single_outfit(
    outfit_card: dict[str, Any],
    gender: str | None,
    cfg: dict[str, Any],
    *,
    person_image_override: str | None = None,
) -> tuple[str | None, str]:
    """对单套搭配执行虚拟试穿（submit → poll 串行，供外部直接调用）。

    返回 (结果图URL, 原因说明)。成功时原因为空字符串，失败时为失败原因。
    person_image_override: 用户自定义模特图（base64 或 URL），优先级高于配置中的预设图。
    """
    oid = outfit_card.get("outfit_id")
    mcfg = (cfg.get("models") or {}).get("tryon_llm") or {}
    base_url = (mcfg.get("base_url") or "").rstrip("/")
    key_env = mcfg.get("api_key_env") or "ANTA_LLM_API_KEY"
    token = (env_or_empty(key_env) or os.environ.get("ANTA_LLM_API_KEY", "")).strip()
    model_id = mcfg.get("model") or "vertex-tryon"
    timeout = float(mcfg.get("timeout_sec") or 180)
    poll_interval = float(mcfg.get("poll_interval_sec") or 5)
    max_attempts = int(mcfg.get("max_poll_attempts") or 60)

    if not base_url or not token:
        reason = f"缺少配置: base_url={'有' if base_url else '无'}, token={'有' if token else '无'}"
        logger.warning("tryon: outfit %s %s", oid, reason)
        return None, reason

    person_image = _resolve_person_image(gender, cfg, person_image_override)
    if not person_image:
        reason = f"未配置person_image, gender={gender}"
        logger.warning("tryon: outfit %s %s", oid, reason)
        return None, reason

    product_image, err = _prepare_outfit_product_image(outfit_card, oid)
    if err:
        return None, err

    try:
        pid = _tryon_submit(
            base_url=base_url,
            token=token,
            person_image=person_image,
            product_image=product_image,
            model_id=model_id,
            timeout=timeout,
        )
        result = _tryon_poll(
            base_url=base_url,
            token=token,
            prediction_id=pid,
            poll_interval=poll_interval,
            max_attempts=max_attempts,
            timeout=timeout,
        )
        image = _extract_result_image(result)
        if not image:
            reason = f"API返回succeeded但未解析到结果图, response={json.dumps(result, ensure_ascii=False)[:300]}"
            logger.warning("tryon: outfit %s %s", oid, reason)
            return None, reason
        logger.info("tryon: outfit %s 试穿成功, image=%s", oid, image[:120])
        return image, ""
    except Exception as exc:
        reason = f"试穿异常: {type(exc).__name__}: {exc}"
        logger.warning("tryon: outfit %s %s", oid, reason, exc_info=True)
        return None, reason


def _resolve_person_image(
    gender: str | None,
    cfg: dict[str, Any],
    person_image_override: str | None = None,
) -> str:
    """解析模特图：优先 override，否则按性别从配置取。"""
    if person_image_override:
        logger.info("tryon: 使用用户自定义模特图")
        return person_image_override
    tryon_cfg = (cfg.get("recommend") or {}).get("tryon") or {}
    person_images = tryon_cfg.get("person_images") or {}
    g = (gender or "").strip()
    gender_map = {"男": "male", "女": "female", "男童": "boy", "女童": "girl", "儿童": "boy"}
    key = gender_map.get(g, g.lower() if g else "male")
    url = person_images.get(key)
    if url:
        return str(url)
    return str(person_images.get("male") or "")


def batch_tryon_outfits(
    outfit_cards: list[dict[str, Any]],
    gender: str | None,
    cfg: dict[str, Any],
    *,
    replace_existing: bool = False,
    max_workers: int | None = None,
    person_image_override: str | None = None,
) -> list[dict[str, Any]]:
    """并行对多套搭配执行虚拟试穿（fire-and-gather 模式）。

    三阶段流水线：
      1. 并行准备：下载图片 + 拼接 → product_image
      2. 并行提交：所有搭配同时 submit → prediction_id
      3. 统一轮询：批量 poll 所有 pending 任务直到全部完成

    返回 [{"outfit_id": ..., "tryon_image": ..., "status": ..., "reason": ...}, ...]。
    status: "success" | "failed" | "skipped"
    person_image_override: 用户自定义模特图，传入后所有搭配共用此图。
    """
    mcfg = (cfg.get("models") or {}).get("tryon_llm") or {}
    base_url = (mcfg.get("base_url") or "").rstrip("/")
    key_env = mcfg.get("api_key_env") or "ANTA_LLM_API_KEY"
    token = (env_or_empty(key_env) or os.environ.get("ANTA_LLM_API_KEY", "")).strip()
    model_id = mcfg.get("model") or "vertex-tryon"
    timeout = float(mcfg.get("timeout_sec") or 180)
    poll_interval = float(mcfg.get("poll_interval_sec") or 5)
    max_attempts = int(mcfg.get("max_poll_attempts") or 60)

    tryon_cfg = (cfg.get("recommend") or {}).get("tryon") or {}
    if max_workers is None:
        max_workers = int(tryon_cfg.get("max_workers") or 3)

    results: list[dict[str, Any]] = []
    tasks: list[tuple[int, dict[str, Any]]] = []
    for i, oc in enumerate(outfit_cards):
        has_existing = bool((oc.get("outfit_tryon_image") or "").strip())
        if has_existing and not replace_existing:
            results.append({
                "outfit_id": oc.get("outfit_id"),
                "index": i,
                "tryon_image": "",
                "status": "skipped",
                "reason": "已有outfit_tryon_image且replace_existing=false",
            })
            continue
        tasks.append((i, oc))

    if not tasks:
        logger.info("tryon: 无需试穿的搭配（全部跳过）, skipped=%d", len(results))
        return results

    if not base_url or not token:
        reason = f"缺少配置: base_url={'有' if base_url else '无'}, token={'有' if token else '无'}"
        for i, oc in tasks:
            results.append({
                "outfit_id": oc.get("outfit_id"),
                "index": i,
                "tryon_image": "",
                "status": "failed",
                "reason": reason,
            })
        return results

    person_image = _resolve_person_image(gender, cfg, person_image_override)
    if not person_image:
        reason = f"未配置person_image, gender={gender}"
        for i, oc in tasks:
            results.append({
                "outfit_id": oc.get("outfit_id"),
                "index": i,
                "tryon_image": "",
                "status": "failed",
                "reason": reason,
            })
        return results

    logger.info(
        "tryon: 批量试穿开始, total=%d, max_workers=%d, gender=%s",
        len(tasks), max_workers, gender,
    )

    # ── 阶段 1：并行准备 product_image ──
    t_prep = time.perf_counter()
    prepared: dict[int, str] = {}
    prep_errors: dict[int, str] = {}

    def _prepare_one(idx: int, card: dict[str, Any]) -> tuple[int, str | None, str]:
        oid = card.get("outfit_id")
        b64, err = _prepare_outfit_product_image(card, oid)
        return idx, b64, err

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_prepare_one, i, oc): i for i, oc in tasks}
        for fut in as_completed(futures):
            idx, b64, err = fut.result()
            if err:
                prep_errors[idx] = err
            else:
                prepared[idx] = b64

    prep_ms = int((time.perf_counter() - t_prep) * 1000)
    logger.info(
        "tryon: 阶段1-准备完成, prepared=%d, errors=%d, elapsed=%dms",
        len(prepared), len(prep_errors), prep_ms,
    )

    for idx, err in prep_errors.items():
        results.append({
            "outfit_id": outfit_cards[idx].get("outfit_id") if idx < len(outfit_cards) else None,
            "index": idx,
            "tryon_image": "",
            "status": "failed",
            "reason": err,
        })

    if not prepared:
        return results

    # ── 阶段 2：并行提交 ──
    t_submit = time.perf_counter()
    pid_map: dict[str, int] = {}
    submit_errors: dict[int, str] = {}

    def _submit_one(idx: int, b64: str) -> tuple[int, str | None, str | None]:
        try:
            pid = _tryon_submit(
                base_url=base_url,
                token=token,
                person_image=person_image,
                product_image=b64,
                model_id=model_id,
                timeout=timeout,
            )
            return idx, pid, None
        except Exception as exc:
            return idx, None, f"提交异常: {type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_submit_one, idx, b64): idx
            for idx, b64 in prepared.items()
        }
        for fut in as_completed(futures):
            idx, pid, err = fut.result()
            if err:
                submit_errors[idx] = err
            elif pid:
                pid_map[pid] = idx

    submit_ms = int((time.perf_counter() - t_submit) * 1000)
    logger.info(
        "tryon: 阶段2-提交完成, submitted=%d, errors=%d, elapsed=%dms",
        len(pid_map), len(submit_errors), submit_ms,
    )

    for idx, err in submit_errors.items():
        results.append({
            "outfit_id": outfit_cards[idx].get("outfit_id") if idx < len(outfit_cards) else None,
            "index": idx,
            "tryon_image": "",
            "status": "failed",
            "reason": err,
        })

    if not pid_map:
        return results

    # ── 阶段 3：统一批量轮询 ──
    t_poll = time.perf_counter()
    pending = {pid: str(outfit_cards[idx].get("outfit_id") or "") for pid, idx in pid_map.items()}
    poll_results = _tryon_poll_batch(
        base_url=base_url,
        token=token,
        pending=pending,
        poll_interval=poll_interval,
        max_attempts=max_attempts,
        timeout=timeout,
    )
    poll_ms = int((time.perf_counter() - t_poll) * 1000)
    logger.info("tryon: 阶段3-轮询完成, elapsed=%dms", poll_ms)

    for pid, data in poll_results.items():
        idx = pid_map.get(pid)
        if idx is None:
            continue
        if data.get("_error"):
            status_text = data.get("status", "unknown")
            body_preview = json.dumps(data.get("body", {}), ensure_ascii=False)[:300]
            results.append({
                "outfit_id": outfit_cards[idx].get("outfit_id") if idx < len(outfit_cards) else None,
                "index": idx,
                "tryon_image": "",
                "status": "failed",
                "reason": f"试穿{status_text}: {body_preview}",
            })
        else:
            image = _extract_result_image(data)
            if image:
                results.append({
                    "outfit_id": outfit_cards[idx].get("outfit_id") if idx < len(outfit_cards) else None,
                    "index": idx,
                    "tryon_image": image,
                    "status": "success",
                    "reason": "",
                })
            else:
                results.append({
                    "outfit_id": outfit_cards[idx].get("outfit_id") if idx < len(outfit_cards) else None,
                    "index": idx,
                    "tryon_image": "",
                    "status": "failed",
                    "reason": f"API返回succeeded但未解析到结果图, response={json.dumps(data, ensure_ascii=False)[:300]}",
                })

    total_ms = prep_ms + submit_ms + poll_ms
    success_n = sum(1 for r in results if r.get("status") == "success")
    logger.info(
        "tryon: 批量试穿完成, total=%d, success=%d, prep=%dms, submit=%dms, poll=%dms, total=%dms",
        len(results), success_n, prep_ms, submit_ms, poll_ms, total_ms,
    )

    return results
