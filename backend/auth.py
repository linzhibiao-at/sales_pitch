"""API Key 鉴权 + 进程内限流排队（沿袭 docs/FILA接口鉴权与限流方案.md）。

限流为**进程内**实现（不引 Redis；redis-py 未装/无 Redis 服务），对齐文档 5.5
「Redis 不可用 → 降级为进程内 asyncio 信号量、可用性优先」。多 worker 下限流为
单进程近似，非全局精确。

鉴权与顶层 ``allowed_app_ids`` 叠加：API Key 绑定 ``app_id``，
``/v1/sales-pitch/generate`` 请求体内 ``app_id`` 须与 Key 绑定值一致。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Optional

from fastapi import HTTPException, Request

from backend.config import get_auth_config, load_api_keys

logger = logging.getLogger(__name__)

# 需鉴权的对外接口 → api_name
_ROUTE_API_NAME = {
    "/v1/sales-pitch/generate": "sales_pitch",
}


def route_to_api_name(path: str) -> Optional[str]:
    """路径 → api_name；非鉴权路径返回 None。"""
    return _ROUTE_API_NAME.get(path)


# 鉴权作用于「对外 B2B 契约」接口（LLM 资源消费的 POST 接口）；
# /health、/api/audit/* 为运维接口，浏览器/内网调用方无法持有 API Key，
# 由网络层 ACL 兜底，不强制鉴权。
PROTECTED_PATHS = frozenset(
    {
        "/v1/sales-pitch/generate",
    }
)


def _is_protected(path: str) -> bool:
    return path in PROTECTED_PATHS


def _parse_expires(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


class ApiKeyStore:
    """API Key 白名单（热加载 ``api_keys.yaml``）。"""

    def __init__(self) -> None:
        self._cfg = get_auth_config()

    def reload(self) -> None:
        """强制下次 get 重读（测试用）。"""
        self._cfg = get_auth_config()

    def get(self, api_key: str) -> Optional[dict[str, Any]]:
        """返回归一化后的 key_info，无效/未找到返回 None。"""
        if not api_key:
            return None
        for k in load_api_keys():
            if str(k.get("api_key", "")).strip() != api_key.strip():
                continue
            status = str(k.get("status") or "active").strip().lower()
            if status != "active":
                return None
            expires = _parse_expires(k.get("expires_at"))
            if expires is not None and datetime.now() > expires:
                return None
            rl = k.get("rate_limit") or {}
            defaults = self._cfg["rate_limit"]
            return {
                "api_key": k.get("api_key"),
                "app_id": str(k.get("app_id") or "").strip(),
                "name": k.get("name"),
                "allowed_apis": [str(x) for x in (k.get("allowed_apis") or [])],
                "rate_limit": {
                    "qpm": int(rl.get("qpm") or defaults["default_qpm"]),
                    "daily": int(rl.get("daily") or defaults["default_daily"]),
                    "concurrent": int(
                        rl.get("concurrent") or defaults["default_concurrent"]
                    ),
                    "queue_size": int(
                        rl.get("queue_size") or defaults["default_queue_size"]
                    ),
                    "queue_timeout": int(
                        rl.get("queue_timeout") or defaults["default_queue_timeout"]
                    ),
                },
                "status": status,
                "expires_at": expires,
            }
        return None


class RateLimiter:
    """进程内 QPM + 日调用量限流（per app_id, per worker 近似）。"""

    def __init__(self) -> None:
        # (app_id, bucket_key) -> count
        self._counts: dict[tuple[str, str], int] = {}

    def _now_keys(self) -> tuple[str, str]:
        t = time.localtime()
        min_key = time.strftime("%Y%m%d%H%M", t)
        day_key = time.strftime("%Y%m%d", t)
        return min_key, day_key

    def check(self, app_id: str, qpm: int, daily: int) -> None:
        """超限 raise 429。"""
        min_key, day_key = self._now_keys()
        # 懒清理该 app_id 的旧分钟桶(长度 12 且非当前分钟)
        for k in list(self._counts):
            if k[0] != app_id:
                continue
            if len(k[1]) == 12 and k[1] != min_key:
                self._counts.pop(k, None)
        min_cnt = self._counts.get((app_id, min_key), 0) + 1
        day_cnt = self._counts.get((app_id, day_key), 0) + 1
        if min_cnt > qpm:
            raise HTTPException(
                status_code=429,
                detail=f"rate limit exceeded: {qpm} req/min",
            )
        if day_cnt > daily:
            raise HTTPException(
                status_code=429,
                detail=f"daily limit exceeded: {daily}/day",
            )
        self._counts[(app_id, min_key)] = min_cnt
        self._counts[(app_id, day_key)] = day_cnt

    def reset(self) -> None:
        self._counts.clear()


class _AppQueue:
    """单 app_id 的并发槽 + 排队队列。"""

    def __init__(self, concurrent: int, queue_size: int, queue_timeout: int):
        self.concurrent = concurrent
        self.queue_size = queue_size
        self.queue_timeout = queue_timeout
        self._sem = asyncio.Semaphore(concurrent)
        self.waiting = 0


class ConcurrencyLimiter:
    """并发 + 排队（asyncio.Semaphore, per app_id, per worker 近似）。"""

    def __init__(self) -> None:
        self._apps: dict[str, _AppQueue] = {}

    def _get(self, app_id: str, key_rl: dict) -> _AppQueue:
        q = self._apps.get(app_id)
        if q is None:
            q = _AppQueue(
                key_rl["concurrent"],
                key_rl["queue_size"],
                key_rl["queue_timeout"],
            )
            self._apps[app_id] = q
        return q

    async def acquire(self, app_id: str, key_rl: dict) -> dict[str, Any]:
        """获取执行槽；返回 {queue_status, queue_wait, queue_position}。
        队列满 → 429 queue full；超时 → 429 queue timeout。"""
        q = self._get(app_id, key_rl)
        if q.waiting >= q.queue_size:
            raise HTTPException(
                status_code=429,
                detail="queue full, try again later",
            )
        q.waiting += 1
        position = q.waiting
        queue_status = "queued" if q._sem.locked() else "immediate"
        start = time.time()
        try:
            await asyncio.wait_for(
                q._sem.acquire(), timeout=q.queue_timeout
            )
            wait = round(time.time() - start, 3)
        except asyncio.TimeoutError:
            q.waiting -= 1
            raise HTTPException(
                status_code=429,
                detail=f"queue timeout after {q.queue_timeout}s",
            )
        q.waiting -= 1
        return {
            "queue_status": queue_status,
            "queue_wait": wait,
            "queue_position": position,
        }

    def release(self, app_id: str) -> None:
        q = self._apps.get(app_id)
        if q is not None:
            q._sem.release()


# 进程级单例
_api_key_store = ApiKeyStore()
_rate_limiter = RateLimiter()
_concurrency_limiter = ConcurrencyLimiter()


def get_key_store() -> ApiKeyStore:
    return _api_key_store


def get_rate_limiter() -> RateLimiter:
    return _rate_limiter


def get_concurrency_limiter() -> ConcurrencyLimiter:
    return _concurrency_limiter


async def verify_api_key(request: Request):
    """鉴权依赖（generator）：Key 校验 + allowed_apis + 限流 + 并发排队。

    - ``auth.enabled=false`` → 不拦
    - 无 Key：``log_only`` 仅记日志，否则 401 ``API key required``
    - Key 无效/停用/过期 → 401 ``invalid API key``
    - 接口无权 → 403 ``access denied: api not allowed``
    - 话术生成接口再做 QPM/日量 + 并发排队

    非异常路径必到 yield（保证 FastAPI generator 依赖至少 yield 一次）；
    并发槽在 finally 释放。
    """
    cfg = get_auth_config()
    acquired = False
    try:
        if not cfg["enabled"]:
            yield
            return
        header_name = cfg["header_name"]
        api_key = (request.headers.get(header_name) or "").strip()
        path = request.url.path
        if not _is_protected(path):
            yield
            return

        if not api_key:
            if cfg["log_only"]:
                logger.info("[auth] no %s header (log_only): %s", header_name, path)
                yield
                return
            raise HTTPException(status_code=401, detail="API key required")

        key_info = _api_key_store.get(api_key)
        if key_info is None:
            if cfg["log_only"]:
                logger.info("[auth] invalid key (log_only): %s", path)
                yield
                return
            raise HTTPException(status_code=401, detail="invalid API key")

        api_name = route_to_api_name(path)
        if api_name and api_name not in key_info["allowed_apis"]:
            raise HTTPException(
                status_code=403, detail="access denied: api not allowed"
            )

        request.state.caller = key_info
        app_id = key_info["app_id"]
        rl = key_info["rate_limit"]

        # 仅 POST（LLM 资源）限流
        if path == "/v1/sales-pitch/generate":
            _rate_limiter.check(app_id, rl["qpm"], rl["daily"])
            queue_info = await _concurrency_limiter.acquire(app_id, rl)
            acquired = True
            request.state.queue_info = queue_info
        yield
    finally:
        if acquired:
            app_id = request.state.caller["app_id"]  # type: ignore[index]
            _concurrency_limiter.release(app_id)
