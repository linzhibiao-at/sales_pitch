"""API Key 鉴权 + 进程内限流单测（不依赖重启/ES/LLM）。

覆盖: ApiKeyStore(active/inactive/expired/未知)、route_to_api_name、
RateLimiter(QPM 内通过/超 QPM/日量/重置)、ConcurrencyLimiter(
immediate/queued/queue full/queue timeout/release)。
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from backend.auth import (
    ApiKeyStore,
    ConcurrencyLimiter,
    RateLimiter,
    route_to_api_name,
    verify_api_key,
)

KEY_MG = "ak_a1b2c3d4e5f6789012345678abcdef01"


def _key(status="active", expires_at=None, allowed_apis=("recommend",)):
    return {
        "api_key": KEY_MG,
        "app_id": "micro_guide",
        "name": "test",
        "allowed_apis": list(allowed_apis),
        "rate_limit": {
            "qpm": 2,
            "daily": 5,
            "concurrent": 1,
            "queue_size": 2,
            "queue_timeout": 1,
        },
        "status": status,
        "expires_at": expires_at,
    }


class TestRouteToApiName(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(route_to_api_name("/v1/outfit/recommend"), "recommend")
        self.assertEqual(route_to_api_name("/v1/outfit/regenerate-reason"), "regenerate-reason")
        self.assertEqual(route_to_api_name("/api/outfits"), "get_outfits")
        self.assertEqual(route_to_api_name("/skus/F11W619219FPK"), "get_sku")
        self.assertIsNone(route_to_api_name("/health"))

    def test_get_endpoints_protected(self):
        """ISS-08: GET 接口 (/api/outfits, /skus/*) 也需鉴权。"""
        from backend.auth import _is_protected
        self.assertTrue(_is_protected("/api/outfits"))
        self.assertTrue(_is_protected("/skus/F11W619219FPK"))
        # 前端/调试接口不鉴权
        self.assertFalse(_is_protected("/api/ui-config"))
        self.assertFalse(_is_protected("/api/outfits/sources"))
        self.assertFalse(_is_protected("/spus/123/skus"))
        self.assertFalse(_is_protected("/outfits/abc"))


class TestApiKeyStore(unittest.TestCase):
    def test_valid_active(self):
        with patch("backend.auth.load_api_keys", return_value=[_key()]):
            info = ApiKeyStore().get(KEY_MG)
        self.assertIsNotNone(info)
        self.assertEqual(info["app_id"], "micro_guide")
        self.assertEqual(info["rate_limit"]["qpm"], 2)
        self.assertIsNone(info["expires_at"])

    def test_unknown_key(self):
        with patch("backend.auth.load_api_keys", return_value=[_key()]):
            self.assertIsNone(ApiKeyStore().get("ak_unknown"))

    def test_inactive(self):
        with patch("backend.auth.load_api_keys", return_value=[_key(status="inactive")]):
            self.assertIsNone(ApiKeyStore().get(KEY_MG))

    def test_expired(self):
        with patch("backend.auth.load_api_keys", return_value=[_key(expires_at="2020-01-01")]):
            self.assertIsNone(ApiKeyStore().get(KEY_MG))

    def test_not_yet_expired(self):
        with patch("backend.auth.load_api_keys", return_value=[_key(expires_at="2099-01-01")]):
            self.assertIsNotNone(ApiKeyStore().get(KEY_MG))


class TestRateLimiter(unittest.TestCase):
    def test_within_qpm_ok(self):
        rl = RateLimiter()
        for _ in range(2):
            rl.check("app1", qpm=2, daily=5)

    def test_over_qpm_rejected(self):
        rl = RateLimiter()
        rl.check("app1", qpm=2, daily=5)
        rl.check("app1", qpm=2, daily=5)
        with self.assertRaises(HTTPException) as ctx:
            rl.check("app1", qpm=2, daily=5)
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertIn("rate limit exceeded", ctx.exception.detail)

    def test_daily_rejected(self):
        rl = RateLimiter()
        for _ in range(5):
            rl.check("app2", qpm=999, daily=5)
        with self.assertRaises(HTTPException) as ctx:
            rl.check("app2", qpm=999, daily=5)
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertIn("daily limit", ctx.exception.detail)

    def test_per_app_id_isolation(self):
        rl = RateLimiter()
        rl.check("app1", qpm=1, daily=999)  # app1 用满 qpm
        rl.check("app2", qpm=1, daily=999)  # app2 不受影响
        with self.assertRaises(HTTPException):
            rl.check("app1", qpm=1, daily=999)


class TestConcurrencyLimiter(unittest.TestCase):
    rl = {
        "concurrent": 1,
        "queue_size": 2,
        "queue_timeout": 1,
        "qpm": 999,
        "daily": 999,
    }

    def test_immediate_acquire_and_release(self):
        lim = ConcurrencyLimiter()

        async def go():
            info = await lim.acquire("app1", self.rl)
            self.assertEqual(info["queue_status"], "immediate")
            lim.release("app1")

        asyncio.run(go())

    def test_queue_full(self):
        lim = ConcurrencyLimiter()

        async def go():
            # 占住唯一并发槽
            await lim.acquire("app1", self.rl)
            # 占满队列(queue_size=2): 2 个等待
            t1 = asyncio.create_task(lim.acquire("app1", self.rl))
            t2 = asyncio.create_task(lim.acquire("app1", self.rl))
            await asyncio.sleep(0.05)  # 让 t1/t2 进入排队
            # 第 3 个应 queue full
            with self.assertRaises(HTTPException) as ctx:
                await lim.acquire("app1", self.rl)
            self.assertEqual(ctx.exception.status_code, 429)
            self.assertIn("queue full", ctx.exception.detail)
            t1.cancel()
            t2.cancel()
            try:
                await asyncio.gather(t1, t2, return_exceptions=True)
            except Exception:
                pass

        asyncio.run(go())

    def test_queue_timeout(self):
        lim = ConcurrencyLimiter()

        async def go():
            # 占住唯一槽, 不释放
            await lim.acquire("app1", self.rl)
            # 第二个排队, queue_timeout=1s → 超时 429
            with self.assertRaises(HTTPException) as ctx:
                await lim.acquire("app1", self.rl)
            self.assertEqual(ctx.exception.status_code, 429)
            self.assertIn("queue timeout", ctx.exception.detail)

        asyncio.run(go())

    def test_release_unblocks_queue(self):
        lim = ConcurrencyLimiter()

        async def go():
            await lim.acquire("app1", self.rl)  # 占槽
            t = asyncio.create_task(lim.acquire("app1", self.rl))
            await asyncio.sleep(0.05)  # 进入排队
            lim.release("app1")  # 释放槽
            info = await t  # 排队的拿到槽
            self.assertIn(info["queue_status"], ("queued", "immediate"))
            lim.release("app1")

        asyncio.run(go())


class TestVerifyApiKeyEndpoint(unittest.TestCase):
    """端到端验证 Depends(verify_api_key) 接线（mini app, 真实依赖, mock config/store）。"""

    def _cfg_enabled(self):
        return {
            "enabled": True,
            "header_name": "X-API-Key",
            "keys_file": "config/api_keys.yaml",
            "log_only": False,
            "rate_limit": {
                "default_qpm": 100, "default_daily": 10000,
                "default_concurrent": 5, "default_queue_size": 20,
                "default_queue_timeout": 30,
            },
        }

    def _client(self):
        app = FastAPI()

        @app.post("/v1/outfit/recommend")
        async def rec(request: Request, body: dict, _auth=Depends(verify_api_key)):
            caller = getattr(request.state, "caller", None)
            if caller and body.get("app_id") != caller["app_id"]:
                raise HTTPException(401, "app_id mismatch with API key")
            return {"ok": True}

        return TestClient(app)

    def _patches(self):
        return (
            patch("backend.auth.get_auth_config", side_effect=self._cfg_enabled),
            patch("backend.auth.load_api_keys", return_value=[_key(allowed_apis=("recommend",))]),
            patch("backend.auth._rate_limiter", RateLimiter()),
            patch("backend.auth._concurrency_limiter", ConcurrencyLimiter()),
        )

    def test_no_key_401(self):
        c = self._client()
        with self._patches()[0], self._patches()[1], self._patches()[2], self._patches()[3]:
            r = c.post("/v1/outfit/recommend", json={"app_id": "micro_guide"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("API key required", r.json()["detail"])

    def test_wrong_key_401(self):
        c = self._client()
        with self._patches()[0], self._patches()[1], self._patches()[2], self._patches()[3]:
            r = c.post("/v1/outfit/recommend", json={"app_id": "micro_guide"},
                       headers={"X-API-Key": "ak_wrong"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("invalid API key", r.json()["detail"])

    def test_valid_key_match_200(self):
        c = self._client()
        with self._patches()[0], self._patches()[1], self._patches()[2], self._patches()[3]:
            r = c.post("/v1/outfit/recommend", json={"app_id": "micro_guide"},
                       headers={"X-API-Key": KEY_MG})
        self.assertEqual(r.status_code, 200)

    def test_app_id_mismatch_401(self):
        c = self._client()
        with self._patches()[0], self._patches()[1], self._patches()[2], self._patches()[3]:
            r = c.post("/v1/outfit/recommend", json={"app_id": "wechat_mini"},
                       headers={"X-API-Key": KEY_MG})
        self.assertEqual(r.status_code, 401)
        self.assertIn("app_id mismatch", r.json()["detail"])

    def test_disabled_no_enforcement(self):
        c = self._client()
        with patch("backend.auth.get_auth_config", return_value={**self._cfg_enabled(), "enabled": False}):
            r = c.post("/v1/outfit/recommend", json={"app_id": "micro_guide"})
        self.assertEqual(r.status_code, 200)

    def test_access_denied_403(self):
        c = self._client()
        # Key 仅允许 recommend；这里打 regenerate 路径(另建 mini app)
        app = FastAPI()

        @app.post("/v1/outfit/regenerate-reason")
        async def reg(_auth=Depends(verify_api_key)):
            return {"ok": True}

        with self._patches()[0], self._patches()[1], self._patches()[2], self._patches()[3]:
            r = TestClient(app).post("/v1/outfit/regenerate-reason", json={},
                                     headers={"X-API-Key": KEY_MG})
        self.assertEqual(r.status_code, 403)
        self.assertIn("access denied", r.json()["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)