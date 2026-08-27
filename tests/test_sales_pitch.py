"""营销话术生成（/v1/sales-pitch/generate）单元测试。"""

import asyncio
import unittest

from pydantic import ValidationError

from backend.auth import PROTECTED_PATHS, route_to_api_name
from backend.models import (
    SalesPitchCustomerInfo,
    SalesPitchProductInfo,
    SalesPitchRequest,
)
from backend.services.request_audit import build_sales_pitch_doc
from backend.services.sales_pitch_service import (
    SalesPitchService,
    build_customer_block,
    build_products_block,
    build_requirements_block,
)


# ── 入参模型校验 ──────────────────────────────────────────────


class SalesPitchRequestValidationTest(unittest.TestCase):
    def _base(self, **overrides) -> dict:
        payload = {"app_id": "micro_guide", "products": [{"title": "FILA 卫衣"}]}
        payload.update(overrides)
        return payload

    def test_products_required(self) -> None:
        with self.assertRaises(ValidationError):
            SalesPitchRequest(**self._base(products=[]))

    def test_product_title_required(self) -> None:
        with self.assertRaises(ValidationError):
            SalesPitchRequest(**self._base(products=[{"title": ""}]))

    def test_products_max_10(self) -> None:
        items = [{"title": f"商品{i}"} for i in range(11)]
        with self.assertRaises(ValidationError):
            SalesPitchRequest(**self._base(products=items))

    def test_max_length_negative_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            SalesPitchRequest(**self._base(max_length=-1))

    def test_max_length_zero_ok(self) -> None:
        req = SalesPitchRequest(**self._base(max_length=0))
        self.assertEqual(req.max_length, 0)

    def test_style_channel_stripped(self) -> None:
        req = SalesPitchRequest(
            **self._base(pitch_style=" warm ", channel=" wechat "),
        )
        self.assertEqual(req.pitch_style, "warm")
        self.assertEqual(req.channel, "wechat")

    def test_html_tags_stripped_in_free_text(self) -> None:
        req = SalesPitchRequest(**self._base(
            customer={"notes": "<script>alert(1)</script>关注面料舒适度"},
            products=[{"title": "卫衣<b>经典</b>", "selling_points": "纯棉<i>透气</i>"}],
        ))
        assert req.customer is not None
        # script 块含内容整体剥除；其余标签仅剥标签本身
        self.assertEqual(req.customer.notes, "关注面料舒适度")
        self.assertEqual(req.products[0].title, "卫衣经典")
        self.assertEqual(req.products[0].selling_points, "纯棉透气")

    def test_title_empty_after_sanitize_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            SalesPitchRequest(**self._base(products=[{"title": "<b></b>"}]))

    def test_surrogate_stripped(self) -> None:
        p = SalesPitchProductInfo(title="卫衣\ud800")
        self.assertEqual(p.title, "卫衣")

    def test_customer_optional(self) -> None:
        req = SalesPitchRequest(**self._base())
        self.assertIsNone(req.customer)


# ── prompt 文本块构建（纯函数）────────────────────────────────


class BuildCustomerBlockTest(unittest.TestCase):
    def test_full_fields_and_extra(self) -> None:
        c = SalesPitchCustomerInfo(
            nickname="王女士", gender="女", age="35",
            style_preference="简约通勤", scene="秋季通勤",
            notes="怕冷，关注面料", extra={"会员等级": "VIP"},
        )
        b = build_customer_block(c)
        self.assertTrue(b.startswith("【顾客信息】"))
        self.assertIn("称呼: 王女士", b)
        self.assertIn("性别: 女", b)
        self.assertIn("风格偏好: 简约通勤", b)
        self.assertIn("备注: 怕冷，关注面料", b)
        self.assertIn("会员等级: VIP", b)

    def test_none_customer_empty(self) -> None:
        self.assertEqual(build_customer_block(None), "")

    def test_all_fields_empty(self) -> None:
        self.assertEqual(build_customer_block(SalesPitchCustomerInfo()), "")

    def test_extra_non_str_value_jsonified(self) -> None:
        c = SalesPitchCustomerInfo(extra={"历史购买": ["卫衣", "运动鞋"]})
        b = build_customer_block(c)
        self.assertIn("历史购买: [\"卫衣\", \"运动鞋\"]", b)


class BuildProductsBlockTest(unittest.TestCase):
    def test_product_lines(self) -> None:
        p = SalesPitchProductInfo(
            sku_id="U2D240211", title="FILA 经典卫衣", price=399.0,
            category="卫衣", color="奶白色", material="棉",
            selling_points="重磅面料; 经典LOGO", extra={"适用季节": "秋冬"},
        )
        b = build_products_block([p])
        self.assertTrue(b.startswith("【商品信息】"))
        self.assertIn("1. FILA 经典卫衣（货号 U2D240211）", b)
        self.assertIn("价格: ¥399", b)
        self.assertIn("类目: 卫衣", b)
        self.assertIn("颜色: 奶白色", b)
        self.assertIn("材质: 棉", b)
        self.assertIn("卖点: 重磅面料; 经典LOGO", b)
        self.assertIn("适用季节: 秋冬", b)

    def test_price_decimal_kept(self) -> None:
        p = SalesPitchProductInfo(title="T恤", price=129.5)
        self.assertIn("价格: ¥129.5", build_products_block([p]))

    def test_multiple_products_indexed(self) -> None:
        ps = [
            SalesPitchProductInfo(title="上装"),
            SalesPitchProductInfo(title="下装"),
        ]
        b = build_products_block(ps)
        self.assertIn("1. 上装", b)
        self.assertIn("2. 下装", b)

    def test_minimal_product(self) -> None:
        b = build_products_block([SalesPitchProductInfo(title="袜子")])
        self.assertEqual(b, "【商品信息】\n1. 袜子")


class BuildRequirementsBlockTest(unittest.TestCase):
    def _req(self, **overrides) -> SalesPitchRequest:
        payload = {
            "app_id": "micro_guide",
            "products": [{"title": "t"}],
        }
        payload.update(overrides)
        return SalesPitchRequest(**payload)

    def test_preset_style_and_channel_mapped(self) -> None:
        b = build_requirements_block(self._req(
            pitch_style="warm", channel="wechat", max_length=120,
        ))
        self.assertTrue(b.startswith("【话术要求】"))
        self.assertIn("风格: 热情亲切", b)
        self.assertIn("渠道: 微信", b)
        self.assertIn("长度: 120 字以内", b)

    def test_free_style_passthrough(self) -> None:
        b = build_requirements_block(self._req(pitch_style="小红书种草风"))
        self.assertIn("风格: 小红书种草风", b)

    def test_no_requirements_empty(self) -> None:
        self.assertEqual(build_requirements_block(self._req()), "")

    def test_max_length_zero_means_unlimited(self) -> None:
        b = build_requirements_block(self._req(max_length=0))
        self.assertNotIn("长度", b)


# ── Mock helpers（DeepAgent Agent mock）────────────────────


class _MockAIMessage:
    """模拟 LangChain AIMessage / HumanMessage。"""

    def __init__(self, content: str, msg_type: str = "ai") -> None:
        self.content = content
        self.type = msg_type


class _MockAgent:
    """模拟 DeepAgent CompiledGraph，支持 ``async ainvoke()``。"""

    def __init__(self, response: str = "", *, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.last_input: dict | None = None
        self.last_config: dict | None = None
        self.call_count = 0

    async def ainvoke(self, input_dict: dict, *, config: dict | None = None) -> dict:
        self.last_input = input_dict
        self.last_config = config
        self.call_count += 1
        if self._error is not None:
            raise self._error
        messages = list(input_dict.get("messages", []))
        if self._response:
            messages.append(_MockAIMessage(self._response, "ai"))
        else:
            messages.append(_MockAIMessage("", "human"))
        return {"messages": messages}


# ── 服务层（mock Agent + stub 审计）──────────────────────


class _FakeAudit:
    def __init__(self) -> None:
        self.docs: list[dict] = []
        self.enabled = True

    def write(self, doc: dict) -> None:
        self.docs.append(doc)


def _make_svc(agent_response: str = "", *, agent_error: Exception | None = None):
    """创建带 mock Agent + stub 审计的 SalesPitchService。"""
    mock_agent = _MockAgent(agent_response, error=agent_error)
    svc = SalesPitchService.__new__(SalesPitchService)
    svc._agent = mock_agent
    svc._audit = _FakeAudit()
    return svc, mock_agent


def _req(**overrides) -> SalesPitchRequest:
    payload = {
        "app_id": "micro_guide",
        "session_id": "sid-1",
        "customer": {"nickname": "王女士", "scene": "秋季通勤"},
        "products": [{
            "sku_id": "U2D240211", "title": "FILA 经典卫衣",
            "price": 399, "selling_points": "重磅面料",
        }],
        "pitch_style": "warm",
        "channel": "wechat",
        "max_length": 120,
    }
    payload.update(overrides)
    return SalesPitchRequest(**payload)


class SalesPitchServiceGenerateTest(unittest.TestCase):
    def test_ok_result_and_audit(self) -> None:
        svc, mock_agent = _make_svc("王女士，这件卫衣非常适合您的秋季通勤~")
        out = asyncio.run(svc.generate(
            _req(), trace_id="tid", app_id="micro_guide", caller="micro_guide",
        ))
        self.assertNotIn("error", out)
        self.assertEqual(out["pitch"], "王女士，这件卫衣非常适合您的秋季通勤~")
        self.assertEqual(out["session_id"], "sid-1")
        self.assertEqual(out["pitch_style"], "warm")
        self.assertIsInstance(out["model"], str)
        # Agent 被调用一次，thread_id 映射到 session_id
        self.assertEqual(mock_agent.call_count, 1)
        self.assertEqual(
            mock_agent.last_config, {"configurable": {"thread_id": "sid-1"}},
        )
        # 用户消息包含三个文本块
        user_msg = mock_agent.last_input["messages"][0]["content"]
        self.assertIn("称呼: 王女士", user_msg)
        self.assertIn("FILA 经典卫衣", user_msg)
        self.assertIn("风格: 热情亲切", user_msg)
        # 审计落库
        self.assertEqual(len(svc._audit.docs), 1)
        doc = svc._audit.docs[0]
        self.assertEqual(doc["request_kind"], "sales_pitch")
        self.assertEqual(doc["status"], "ok")
        self.assertEqual(doc["trace_id"], "tid")
        self.assertEqual(doc["input"]["customer"]["nickname"], "王女士")
        self.assertEqual(doc["input"]["products"][0]["title"], "FILA 经典卫衣")
        self.assertEqual(doc["result"]["pitch"], out["pitch"])

    def test_empty_agent_output_returns_error_doc(self) -> None:
        svc, _ = _make_svc("")  # 空响应 → 无 AI 消息
        out = asyncio.run(svc.generate(_req(), trace_id="tid"))
        self.assertIn("error", out)
        doc = svc._audit.docs[0]
        self.assertEqual(doc["status"], "error")
        self.assertEqual(doc["result"]["error"], out["error"])

    def test_exception_reraised_and_audited(self) -> None:
        svc, _ = _make_svc("", agent_error=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            asyncio.run(svc.generate(_req(), trace_id="tid"))
        doc = svc._audit.docs[0]
        self.assertEqual(doc["status"], "error")
        self.assertIn("RuntimeError", str(doc["error"]))

    def test_audit_disabled_skips_write(self) -> None:
        svc, _ = _make_svc("话术")
        svc._audit.enabled = False
        asyncio.run(svc.generate(_req(), trace_id="tid"))
        self.assertEqual(svc._audit.docs, [])

    def test_no_customer_still_works(self) -> None:
        svc, mock_agent = _make_svc("通用话术")
        out = asyncio.run(svc.generate(
            _req(customer=None), trace_id="tid",
        ))
        self.assertEqual(out["pitch"], "通用话术")
        user_msg = mock_agent.last_input["messages"][0]["content"]
        self.assertNotIn("【顾客信息】", user_msg)


# ── 审计文档构建（纯函数）────────────────────────────────────


class BuildSalesPitchDocTest(unittest.TestCase):
    def _meta(self, **overrides) -> dict:
        meta = {
            "trace_id": "tid", "session_id": "sid", "app_id": "app",
            "caller": "caller", "ts": "2026-08-26T10:00:00+08:00",
            "elapsed_ms": 120, "status": "ok", "error": None,
        }
        meta.update(overrides)
        return meta

    def test_ok_shape_and_pitch_truncated(self) -> None:
        pitch = "话" * 800
        doc = build_sales_pitch_doc(
            input_block={"customer": {"nickname": "王女士"}},
            result={"pitch": pitch},
            meta=self._meta(),
        )
        self.assertEqual(doc["request_kind"], "sales_pitch")
        self.assertEqual(doc["status"], "ok")
        self.assertIsNone(doc["intent"])
        self.assertIsNone(doc["recall"])
        self.assertIsNone(doc["ranking"])
        self.assertEqual(doc["result"]["pitch_len"], 800)
        self.assertEqual(len(doc["result"]["pitch"]), 600)

    def test_error_result(self) -> None:
        doc = build_sales_pitch_doc(
            input_block={},
            result={"error": "sales pitch generation failed"},
            meta=self._meta(status="error", error="sales pitch generation failed"),
        )
        self.assertEqual(doc["status"], "error")
        self.assertEqual(doc["result"]["error"], "sales pitch generation failed")
        self.assertEqual(doc["result"]["pitch_len"], 0)

    def test_none_result(self) -> None:
        doc = build_sales_pitch_doc(input_block={}, result=None, meta=self._meta())
        self.assertIsNone(doc["result"])


# ── 鉴权路由注册 ─────────────────────────────────────────────


class SalesPitchAuthRouteTest(unittest.TestCase):
    def test_route_api_name(self) -> None:
        self.assertEqual(
            route_to_api_name("/v1/sales-pitch/generate"), "sales_pitch",
        )

    def test_path_protected(self) -> None:
        self.assertIn("/v1/sales-pitch/generate", PROTECTED_PATHS)


if __name__ == "__main__":
    unittest.main()
