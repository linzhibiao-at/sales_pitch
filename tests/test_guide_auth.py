"""导购身份校验单测（不依赖 MySQL/Redis/LLM）。

覆盖: get_guide_auth_config(段缺失默认/strip 归一化/脏条目丢弃/畸形段降级)、
GuideUserStore(命中/未命中/空白/空列表)、verify_guide_identity(
关闭放行/缺失 400/空白 400/未知 401/命中返回用户)、路由级集成(
应用级鉴权优先/未知 401/缺失 400/关闭放行/命中 200)。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from backend.auth import ConcurrencyLimiter, RateLimiter, verify_api_key
from backend.config import get_guide_auth_config
from backend.guide_auth import (
    GuideUserStore,
    get_guide_user_store,
    verify_guide_identity,
)

KEY_MG = "ak_a1b2c3d4e5f6789012345678abcdef01"


# ── 配置读取（get_guide_auth_config）────────────────────────


class GetGuideAuthConfigTest(unittest.TestCase):
    def test_section_missing_defaults(self):
        """段缺失: enabled=False + 空用户列表（向后兼容, 不校验）。"""
        with patch("backend.config.load_config", return_value={}):
            cfg = get_guide_auth_config()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["users"], [])

    def test_normalization_and_dirty_entries(self):
        """guide_num 剥空白; 空/缺失 guide_num 与非 dict 条目丢弃; 余字段保留。"""
        raw = {
            "guide_auth": {
                "enabled": True,
                "users": [
                    {"guide_num": "  G001  ", "name": "导购一"},
                    {"guide_num": "", "name": "空"},
                    {"guide_num": "   "},
                    {"name": "缺 guide_num"},
                    "bad-entry",
                    {"guide_num": "G002", "status": "active"},
                ],
            }
        }
        with patch("backend.config.load_config", return_value=raw):
            cfg = get_guide_auth_config()
        self.assertTrue(cfg["enabled"])
        self.assertEqual([u["guide_num"] for u in cfg["users"]], ["G001", "G002"])
        self.assertEqual(cfg["users"][0]["name"], "导购一")
        self.assertEqual(cfg["users"][1]["status"], "active")

    def test_malformed_section_and_users(self):
        """段非 dict / users 非列表: 降级为空列表, 不抛异常。"""
        with patch("backend.config.load_config", return_value={"guide_auth": "oops"}):
            cfg = get_guide_auth_config()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["users"], [])
        with patch(
            "backend.config.load_config",
            return_value={"guide_auth": {"enabled": True, "users": "oops"}},
        ):
            cfg = get_guide_auth_config()
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["users"], [])

    def test_enabled_default_false(self):
        """enabled 键缺失: 默认 False（键存在与否与 users 归一化互不影响）。"""
        with patch(
            "backend.config.load_config",
            return_value={"guide_auth": {"users": [{"guide_num": "G001"}]}},
        ):
            cfg = get_guide_auth_config()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(len(cfg["users"]), 1)


# ── 组件（GuideUserStore / verify_guide_identity）──────────────


def _guide_cfg(enabled: bool = True, users: list | None = None) -> dict:
    return {
        "enabled": enabled,
        "users": users if users is not None else [
            {"guide_num": "G001", "name": "测试导购一"},
        ],
    }


class GuideUserStoreTest(unittest.TestCase):
    def test_get_hit(self):
        with patch("backend.guide_auth.get_guide_auth_config",
                   return_value=_guide_cfg()):
            user = GuideUserStore().get("G001")
        self.assertIsNotNone(user)
        self.assertEqual(user["name"], "测试导购一")

    def test_get_miss(self):
        with patch("backend.guide_auth.get_guide_auth_config",
                   return_value=_guide_cfg()):
            self.assertIsNone(GuideUserStore().get("G999"))

    def test_get_empty_users(self):
        """用户列表为空: 任意 guide_num 均未命中。"""
        with patch("backend.guide_auth.get_guide_auth_config",
                   return_value=_guide_cfg(users=[])):
            self.assertIsNone(GuideUserStore().get("G001"))

    def test_get_blank_guide_num(self):
        with patch("backend.guide_auth.get_guide_auth_config",
                   return_value=_guide_cfg()):
            self.assertIsNone(GuideUserStore().get("   "))

    def test_singleton_getter(self):
        self.assertIsInstance(get_guide_user_store(), GuideUserStore)


class VerifyGuideIdentityTest(unittest.TestCase):
    def test_disabled_passthrough(self):
        """开关关闭: 缺失/未知 guide_num 均放行（返回 None）。"""
        with patch("backend.guide_auth.get_guide_auth_config",
                   return_value=_guide_cfg(enabled=False)):
            self.assertIsNone(verify_guide_identity(None))
            self.assertIsNone(verify_guide_identity("G999"))

    def test_missing_guide_num_400(self):
        with patch("backend.guide_auth.get_guide_auth_config",
                   return_value=_guide_cfg()):
            with self.assertRaises(HTTPException) as ctx:
                verify_guide_identity(None)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "guide_num required")

    def test_blank_guide_num_400(self):
        """空白字符串视为缺失 → 400。"""
        with patch("backend.guide_auth.get_guide_auth_config",
                   return_value=_guide_cfg()):
            with self.assertRaises(HTTPException) as ctx:
                verify_guide_identity("   ")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "guide_num required")

    def test_unknown_guide_num_401(self):
        with patch("backend.guide_auth.get_guide_auth_config",
                   return_value=_guide_cfg()):
            with self.assertRaises(HTTPException) as ctx:
                verify_guide_identity("G999")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, "guide not found")

    def test_hit_returns_user(self):
        with patch("backend.guide_auth.get_guide_auth_config",
                   return_value=_guide_cfg()):
            user = verify_guide_identity(" G001 ")
        self.assertEqual(user["guide_num"], "G001")


# ── 路由级集成（应用级鉴权 + 导购校验接线, 仿 test_auth.py）─────


class GuideIdentityRouteTest(unittest.TestCase):
    """端到端验证 Depends(verify_api_key) + handler 内 verify_guide_identity
    接线顺序: 应用级鉴权优先, 导购校验次之（design.md D1）。
    """

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

    def _key(self):
        return {
            "api_key": KEY_MG,
            "app_id": "micro_guide",
            "name": "test",
            "allowed_apis": ["sales_pitch"],
            "rate_limit": {
                "qpm": 2, "daily": 5, "concurrent": 1,
                "queue_size": 2, "queue_timeout": 1,
            },
            "status": "active",
            "expires_at": None,
        }

    def _client(self):
        app = FastAPI()

        @app.post("/v1/sales-pitch/generate")
        async def pitch(request: Request, body: dict, _auth=Depends(verify_api_key)):
            caller = getattr(request.state, "caller", None)
            if caller and body.get("app_id") != caller["app_id"]:
                raise HTTPException(401, "app_id mismatch with API key")
            guide = verify_guide_identity(body.get("guide_num"))
            if guide is not None:
                request.state.guide = guide
            return {"ok": True, "guide_num": (guide or {}).get("guide_num")}

        return TestClient(app)

    def _post(self, body: dict, guide_cfg: dict, headers: dict | None = None):
        c = self._client()
        p = (
            patch("backend.auth.get_auth_config", side_effect=self._cfg_enabled),
            patch("backend.auth.load_api_keys", return_value=[self._key()]),
            patch("backend.auth._rate_limiter", RateLimiter()),
            patch("backend.auth._concurrency_limiter", ConcurrencyLimiter()),
            patch("backend.guide_auth.get_guide_auth_config",
                  return_value=guide_cfg),
        )
        with p[0], p[1], p[2], p[3], p[4]:
            return c.post(
                "/v1/sales-pitch/generate",
                json=body, headers=headers or {},
            )

    def test_invalid_api_key_takes_priority(self):
        """无效 API Key + 合法 guide_num → 401 invalid API key（应用级优先）。"""
        r = self._post(
            {"app_id": "micro_guide", "guide_num": "G001"},
            _guide_cfg(), headers={"X-API-Key": "ak_wrong"},
        )
        self.assertEqual(r.status_code, 401)
        self.assertIn("invalid API key", r.json()["detail"])

    def test_unknown_guide_401(self):
        r = self._post(
            {"app_id": "micro_guide", "guide_num": "G999"},
            _guide_cfg(), headers={"X-API-Key": KEY_MG},
        )
        self.assertEqual(r.status_code, 401)
        self.assertIn("guide not found", r.json()["detail"])

    def test_missing_guide_400(self):
        r = self._post(
            {"app_id": "micro_guide"},
            _guide_cfg(), headers={"X-API-Key": KEY_MG},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("guide_num required", r.json()["detail"])

    def test_disabled_without_guide_200(self):
        """开关关闭 + 无 guide_num → 200 放行（向后兼容）。"""
        r = self._post(
            {"app_id": "micro_guide"},
            _guide_cfg(enabled=False), headers={"X-API-Key": KEY_MG},
        )
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["guide_num"])

    def test_valid_guide_200(self):
        """命中导购（含前后空白剥离）→ 200 且 request.state.guide 接线生效。"""
        r = self._post(
            {"app_id": "micro_guide", "guide_num": " G001 "},
            _guide_cfg(), headers={"X-API-Key": KEY_MG},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["guide_num"], "G001")


if __name__ == "__main__":
    unittest.main(verbosity=2)
