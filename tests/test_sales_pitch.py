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
    build_coupons_block,
    build_customer_block,
    build_extra_prompt_block,
    build_products_block,
    build_promotions_block,
    build_requirements_block,
)


# ── 入参模型校验 ──────────────────────────────────────────────


class SalesPitchRequestValidationTest(unittest.TestCase):
    def _base(self, **overrides) -> dict:
        payload = {
            "app_id": "micro_guide",
            "guide_num": "G001",
            "customer": {"union_id": "U_test123"},
            "products": [{"title": "FILA 卫衣"}],
        }
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

    def test_style_stripped(self) -> None:
        req = SalesPitchRequest(**self._base(pitch_style=" warm "))
        self.assertEqual(req.pitch_style, "warm")

    def test_extra_prompt_optional_default_none(self) -> None:
        req = SalesPitchRequest(**self._base())
        self.assertIsNone(req.extra_prompt)

    def test_extra_prompt_stripped(self) -> None:
        req = SalesPitchRequest(**self._base(extra_prompt=" 突出秋冬新品 "))
        self.assertEqual(req.extra_prompt, "突出秋冬新品")

    def test_extra_prompt_whitespace_means_not_provided(self) -> None:
        req = SalesPitchRequest(**self._base(extra_prompt="   "))
        self.assertEqual(req.extra_prompt, "")

    def test_extra_prompt_max_500_ok(self) -> None:
        req = SalesPitchRequest(**self._base(extra_prompt="要" * 500))
        self.assertEqual(len(req.extra_prompt or ""), 500)

    def test_extra_prompt_over_500_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            SalesPitchRequest(**self._base(extra_prompt="要" * 501))

    def test_extra_prompt_html_and_surrogate_stripped(self) -> None:
        req = SalesPitchRequest(**self._base(
            extra_prompt="<script>alert(1)</script>突出新品<em>上市</em>\ud800",
        ))
        # script 块含内容整体剥除；其余标签仅剥标签本身；surrogate 剔除
        self.assertEqual(req.extra_prompt, "突出新品上市")

    def test_guide_num_required(self) -> None:
        """guide_num 必传: 不传报 422。"""
        payload = {"app_id": "micro_guide", "customer": {"union_id": "U1"}, "products": [{"title": "t"}]}
        with self.assertRaises(ValidationError):
            SalesPitchRequest(**payload)

    def test_guide_num_stripped(self) -> None:
        req = SalesPitchRequest(**self._base(guide_num=" G001 "))
        self.assertEqual(req.guide_num, "G001")

    def test_html_tags_stripped_in_free_text(self) -> None:
        req = SalesPitchRequest(**self._base(
            customer={"union_id": "U1", "nickname": "<script>alert(1)</script>王女士"},
            products=[{"title": "卫衣<b>经典</b>"}],
        ))
        assert req.customer is not None
        # script 块含内容整体剥除；其余标签仅剥标签本身
        self.assertEqual(req.customer.nickname, "王女士")
        self.assertEqual(req.products[0].title, "卫衣经典")

    def test_title_empty_after_sanitize_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            SalesPitchRequest(**self._base(products=[{"title": "<b></b>"}]))

    def test_surrogate_stripped(self) -> None:
        p = SalesPitchProductInfo(title="卫衣\ud800")
        self.assertEqual(p.title, "卫衣")

    def test_customer_required(self) -> None:
        """customer 必传: 不传报 422。"""
        payload = {"app_id": "micro_guide", "guide_num": "G001", "products": [{"title": "t"}]}
        with self.assertRaises(ValidationError):
            SalesPitchRequest(**payload)

    def test_customer_union_id_required(self) -> None:
        """customer.union_id 必传: 缺失或空字符串报 422。"""
        with self.assertRaises(ValidationError):
            SalesPitchRequest(**self._base(customer={}))
        with self.assertRaises(ValidationError):
            SalesPitchRequest(**self._base(customer={"union_id": ""}))

    def test_member_level_points_optional_default_none(self) -> None:
        req = SalesPitchRequest(**self._base())
        assert req.customer is not None
        self.assertIsNone(req.customer.member_level)
        self.assertIsNone(req.customer.points)

    def test_member_level_points_carried(self) -> None:
        req = SalesPitchRequest(**self._base(
            customer={"union_id": "U1", "member_level": "金卡会员", "points": 1260},
        ))
        assert req.customer is not None
        self.assertEqual(req.customer.member_level, "金卡会员")
        self.assertEqual(req.customer.points, 1260)

    def test_points_negative_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            SalesPitchRequest(**self._base(
                customer={"union_id": "U1", "points": -5},
            ))

    def test_member_level_html_and_surrogate_stripped(self) -> None:
        req = SalesPitchRequest(**self._base(
            customer={"union_id": "U1", "member_level": "<b>金卡</b>会员\ud800"},
        ))
        assert req.customer is not None
        self.assertEqual(req.customer.member_level, "金卡会员")


class PromotionsValidationTest(unittest.TestCase):
    """promotions 已选活动字段校验（1.2）。"""

    def _base(self, **overrides) -> dict:
        payload = {
            "app_id": "micro_guide",
            "guide_num": "G001",
            "customer": {"union_id": "U_test123"},
            "products": [{"title": "FILA 卫衣"}],
        }
        payload.update(overrides)
        return payload

    def test_optional_default_none(self) -> None:
        req = SalesPitchRequest(**self._base())
        self.assertIsNone(req.promotions)

    def test_carried(self) -> None:
        req = SalesPitchRequest(**self._base(promotions=[
            {"promo_id": "mixian", "name": "万象城1000-200",
             "copy": "现在万象城有【部分整单满减】满1000减200。"},
        ]))
        assert req.promotions is not None
        self.assertEqual(req.promotions[0].promo_id, "mixian")
        self.assertEqual(req.promotions[0].name, "万象城1000-200")
        # 接口字段名为 copy，内部属性名为 copy_text（alias 保持对外不变）
        self.assertEqual(
            req.promotions[0].copy_text, "现在万象城有【部分整单满减】满1000减200。",
        )

    def test_name_optional(self) -> None:
        req = SalesPitchRequest(**self._base(promotions=[
            {"promo_id": "p1", "copy": "活动文案"},
        ]))
        assert req.promotions is not None
        self.assertIsNone(req.promotions[0].name)

    def test_empty_list_ok(self) -> None:
        req = SalesPitchRequest(**self._base(promotions=[]))
        self.assertEqual(req.promotions, [])

    def test_copy_missing_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            SalesPitchRequest(**self._base(promotions=[{"promo_id": "p1"}]))

    def test_copy_empty_rejected(self) -> None:
        for bad in ("", "   ", "<b></b>"):
            with self.assertRaises(ValidationError):
                SalesPitchRequest(**self._base(
                    promotions=[{"promo_id": "p1", "copy": bad}],
                ))

    def test_promo_id_blank_rejected(self) -> None:
        for bad in ("", "   "):
            with self.assertRaises(ValidationError):
                SalesPitchRequest(**self._base(
                    promotions=[{"promo_id": bad, "copy": "x"}],
                ))

    def test_over_10_rejected(self) -> None:
        items = [{"promo_id": f"p{i}", "copy": "c"} for i in range(11)]
        with self.assertRaises(ValidationError):
            SalesPitchRequest(**self._base(promotions=items))

    def test_copy_html_and_surrogate_stripped(self) -> None:
        req = SalesPitchRequest(**self._base(promotions=[
            {"promo_id": "p1",
             "copy": "<script>alert(1)</script>满1000减200<em>现已开始</em>\ud800"},
        ]))
        assert req.promotions is not None
        self.assertEqual(req.promotions[0].copy_text, "满1000减200现已开始")


class CouponsValidationTest(unittest.TestCase):
    """coupon_names 字段校验（1.3）。"""

    def _base(self, **overrides) -> dict:
        payload = {
            "app_id": "micro_guide",
            "guide_num": "G001",
            "customer": {"union_id": "U_test123"},
            "products": [{"title": "FILA 卫衣"}],
        }
        payload.update(overrides)
        return payload

    def test_coupon_names_optional_default_none(self) -> None:
        req = SalesPitchRequest(**self._base())
        self.assertIsNone(req.coupon_names)

    def test_coupon_names_empty_list_kept(self) -> None:
        """显式空列表与缺省语义不同，清理后仍为 []。"""
        req = SalesPitchRequest(**self._base(coupon_names=[]))
        self.assertEqual(req.coupon_names, [])

    def test_coupon_names_carried_and_cleaned(self) -> None:
        req = SalesPitchRequest(**self._base(coupon_names=[
            " 500元生日券 ", "<b>冬季焕新券</b>", "   ", "券\ud800名",
        ]))
        self.assertEqual(req.coupon_names, ["500元生日券", "冬季焕新券", "券名"])

    def test_coupon_names_all_blank_becomes_empty_list(self) -> None:
        req = SalesPitchRequest(**self._base(coupon_names=[" ", "<b></b>"]))
        self.assertEqual(req.coupon_names, [])

    def test_coupon_names_over_20_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            SalesPitchRequest(**self._base(
                coupon_names=[f"券{i}" for i in range(21)],
            ))

    def test_coupon_names_item_max_100_ok(self) -> None:
        req = SalesPitchRequest(**self._base(coupon_names=["券" * 100]))
        self.assertEqual(req.coupon_names, ["券" * 100])

    def test_coupon_names_item_over_100_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            SalesPitchRequest(**self._base(coupon_names=["券" * 101]))


# ── prompt 文本块构建（纯函数）────────────────────────────────


class BuildCustomerBlockTest(unittest.TestCase):
    def test_full_fields_and_extra(self) -> None:
        c = SalesPitchCustomerInfo(
            union_id="U_full", nickname="王女士", gender="女", age="35",
            member_level="金卡会员", points=1260, extra={"历史购买": "卫衣"},
        )
        b = build_customer_block(c)
        self.assertTrue(b.startswith("【顾客信息】"))
        self.assertIn("称呼: 王女士", b)
        self.assertIn("性别: 女", b)
        self.assertIn("年龄段: 35", b)
        self.assertIn("会员等级: 金卡会员", b)
        self.assertIn("积分: 1260", b)
        self.assertIn("历史购买: 卫衣", b)

    def test_none_customer_empty(self) -> None:
        self.assertEqual(build_customer_block(None), "")

    def test_all_fields_empty(self) -> None:
        self.assertEqual(build_customer_block(SalesPitchCustomerInfo(union_id="U1")), "")

    def test_extra_non_str_value_jsonified(self) -> None:
        c = SalesPitchCustomerInfo(union_id="U1", extra={"历史购买": ["卫衣", "运动鞋"]})
        b = build_customer_block(c)
        self.assertIn("历史购买: [\"卫衣\", \"运动鞋\"]", b)

    def test_member_level_and_points_lines(self) -> None:
        c = SalesPitchCustomerInfo(
            union_id="U1", member_level="金卡会员", points=1260,
        )
        b = build_customer_block(c)
        self.assertIn("会员等级: 金卡会员", b)
        self.assertIn("积分: 1260", b)


class BuildProductsBlockTest(unittest.TestCase):
    def test_product_lines(self) -> None:
        p = SalesPitchProductInfo(
            title="FILA 经典卫衣", color="奶白色", extra={"适用季节": "秋冬"},
        )
        b = build_products_block([p])
        self.assertTrue(b.startswith("【商品信息】"))
        self.assertIn("1. FILA 经典卫衣", b)
        self.assertIn("颜色: 奶白色", b)
        self.assertIn("适用季节: 秋冬", b)
        # 被移除字段不产生对应行
        for gone in ("货号", "价格:", "类目:", "材质:", "卖点:"):
            self.assertNotIn(gone, b)

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
            "guide_num": "G001",
            "customer": {"union_id": "U1"},
            "products": [{"title": "t"}],
        }
        payload.update(overrides)
        return SalesPitchRequest(**payload)

    def test_preset_style_and_length_mapped(self) -> None:
        b = build_requirements_block(self._req(
            pitch_style="warm", max_length=120,
        ))
        self.assertTrue(b.startswith("【话术要求】"))
        self.assertIn("风格: 热情亲切", b)
        self.assertIn("长度: 120 字以内", b)
        # 被移除字段不产生对应行
        self.assertNotIn("分享方式", b)
        self.assertNotIn("渠道", b)

    def test_free_style_passthrough(self) -> None:
        b = build_requirements_block(self._req(pitch_style="小红书种草风"))
        self.assertIn("风格: 小红书种草风", b)

    def test_no_requirements_empty(self) -> None:
        self.assertEqual(build_requirements_block(self._req()), "")

    def test_max_length_zero_means_unlimited(self) -> None:
        b = build_requirements_block(self._req(max_length=0))
        self.assertNotIn("长度", b)

    def test_four_tone_preset_mapped(self) -> None:
        """四档语气（中文取值）命中预设，风格行输出同名标签（2.2）。"""
        for tone in ("亲切自然", "活力潮流", "专业尊贵", "简约高效"):
            b = build_requirements_block(self._req(pitch_style=tone))
            self.assertIn(f"风格: {tone}", b)

    def test_no_share_mode_or_channel_lines(self) -> None:
        """被移除字段不出现在【话术要求】块。"""
        b = build_requirements_block(
            self._req(pitch_style="活力潮流", max_length=120),
        )
        self.assertNotIn("分享方式", b)
        self.assertNotIn("渠道", b)


class BuildExtraPromptBlockTest(unittest.TestCase):
    def _req(self, extra_prompt: str | None = None) -> SalesPitchRequest:
        payload = {
            "app_id": "micro_guide",
            "guide_num": "G001",
            "customer": {"union_id": "U1"},
            "products": [{"title": "t"}],
        }
        if extra_prompt is not None:
            payload["extra_prompt"] = extra_prompt
        return SalesPitchRequest(**payload)

    def test_single_line(self) -> None:
        b = build_extra_prompt_block(self._req("突出秋冬新品"))
        self.assertEqual(b, "【补充要求】\n突出秋冬新品")

    def test_multi_line_kept(self) -> None:
        b = build_extra_prompt_block(
            self._req("突出秋冬新品\n提醒会员双倍积分"),
        )
        self.assertEqual(b, "【补充要求】\n突出秋冬新品\n提醒会员双倍积分")

    def test_absent_empty(self) -> None:
        self.assertEqual(build_extra_prompt_block(self._req()), "")

    def test_whitespace_empty(self) -> None:
        self.assertEqual(build_extra_prompt_block(self._req("   ")), "")


class BuildPromotionsBlockTest(unittest.TestCase):
    """【促销活动】块构建（2.1）。"""

    def _req(self, promotions=None) -> SalesPitchRequest:
        payload = {
            "app_id": "micro_guide",
            "guide_num": "G001",
            "customer": {"union_id": "U1"},
            "products": [{"title": "t"}],
        }
        if promotions is not None:
            payload["promotions"] = promotions
        return SalesPitchRequest(**payload)

    def test_block_with_name(self) -> None:
        b = build_promotions_block(self._req([
            {"promo_id": "mixian", "name": "万象城1000-200",
             "copy": "现在万象城有【部分整单满减】满1000减200，这套一起买刚好能用上。"},
        ]))
        self.assertTrue(b.startswith("【促销活动】"))
        self.assertIn(
            "- 万象城1000-200: 现在万象城有【部分整单满减】满1000减200，这套一起买刚好能用上。",
            b,
        )

    def test_block_without_name(self) -> None:
        b = build_promotions_block(self._req([
            {"promo_id": "p1", "copy": "两件一起买享 88 折"},
        ]))
        self.assertEqual(b, "【促销活动】\n- 两件一起买享 88 折")

    def test_promo_id_not_injected(self) -> None:
        """promo_id 仅供审计溯源，不进入提示词。"""
        b = build_promotions_block(self._req([
            {"promo_id": "mixian", "copy": "满1000减200"},
        ]))
        self.assertNotIn("mixian", b)

    def test_absent_empty(self) -> None:
        self.assertEqual(build_promotions_block(self._req()), "")

    def test_empty_list_empty(self) -> None:
        self.assertEqual(build_promotions_block(self._req([])), "")

    def test_multiple_items(self) -> None:
        b = build_promotions_block(self._req([
            {"promo_id": "p1", "name": "A", "copy": "文案1"},
            {"promo_id": "p2", "copy": "文案2"},
        ]))
        self.assertIn("- A: 文案1", b)
        self.assertIn("- 文案2", b)


class BuildCouponsBlockTest(unittest.TestCase):
    """【顾客优惠券】块构建（2.1）。"""

    def _req(self, coupon_names=None) -> SalesPitchRequest:
        payload = {
            "app_id": "micro_guide",
            "guide_num": "G001",
            "customer": {"union_id": "U1"},
            "products": [{"title": "t"}],
        }
        if coupon_names is not None:
            payload["coupon_names"] = coupon_names
        return SalesPitchRequest(**payload)

    def test_absent_empty(self) -> None:
        """缺省不注入（旧调用方零差异）。"""
        self.assertEqual(build_coupons_block(self._req()), "")

    def test_names_listed(self) -> None:
        b = build_coupons_block(self._req(["500元生日券", "FUSION38元专属鞋券"]))
        self.assertEqual(
            b, "【顾客优惠券】\n- 可用券: 500元生日券、FUSION38元专属鞋券",
        )

    def test_explicit_empty_list_fallback(self) -> None:
        b = build_coupons_block(self._req([]))
        self.assertTrue(b.startswith("【顾客优惠券】"))
        self.assertIn("暂无可用券", b)
        self.assertIn("会员专属优惠", b)

    def test_cleaned_blank_items_fallback(self) -> None:
        """显式携带但清理后为空 → 同样触发兜底。"""
        b = build_coupons_block(self._req([" ", "<b></b>"]))
        self.assertIn("会员专属优惠", b)


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
        "guide_num": "G001",
        "customer": {"union_id": "U_abc", "nickname": "王女士"},
        "products": [{"title": "FILA 经典卫衣"}],
        "pitch_style": "warm",
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
        # session_id = {guide_num}_{union_id}
        self.assertEqual(out["session_id"], "G001_U_abc")
        self.assertEqual(out["pitch_style"], "warm")
        self.assertIsInstance(out["model"], str)
        # Agent 被调用一次，thread_id 映射到确定性 session_id
        self.assertEqual(mock_agent.call_count, 1)
        self.assertEqual(
            mock_agent.last_config, {"configurable": {"thread_id": "G001_U_abc"}},
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
        self.assertEqual(doc["input"]["guide_num"], "G001")
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

    def test_audit_guide_num_always_present(self) -> None:
        """guide_num 必传: 审计记录始终存在。"""
        svc, _ = _make_svc("话术")
        asyncio.run(svc.generate(_req(), trace_id="tid"))
        doc = svc._audit.docs[0]
        self.assertEqual(doc["input"]["guide_num"], "G001")

    def test_session_id_deterministic(self) -> None:
        """相同 guide_num + union_id → 相同 session_id。"""
        svc1, agent1 = _make_svc("话术")
        asyncio.run(svc1.generate(_req(), trace_id="t1"))
        svc2, agent2 = _make_svc("话术")
        asyncio.run(svc2.generate(_req(), trace_id="t2"))
        self.assertEqual(
            agent1.last_config, agent2.last_config,
        )
        self.assertEqual(
            agent1.last_config["configurable"]["thread_id"], "G001_U_abc",
        )

    def test_minimal_customer_still_works(self) -> None:
        """customer 仅含 union_id 时仍可生成。"""
        svc, mock_agent = _make_svc("通用话术")
        out = asyncio.run(svc.generate(
            _req(customer={"union_id": "U_minimal"}), trace_id="tid",
        ))
        self.assertEqual(out["session_id"], "G001_U_minimal")
        self.assertEqual(out["pitch"], "通用话术")
        user_msg = mock_agent.last_input["messages"][0]["content"]
        self.assertNotIn("称呼:", user_msg)

    def test_extra_prompt_injected_after_requirements(self) -> None:
        """非空 extra_prompt 以【补充要求】块注入，位于【话术要求】之后。"""
        svc, mock_agent = _make_svc("话术")
        asyncio.run(svc.generate(_req(extra_prompt="突出秋冬新品"), trace_id="tid"))
        user_msg = mock_agent.last_input["messages"][0]["content"]
        self.assertIn("【补充要求】\n突出秋冬新品", user_msg)
        self.assertLess(
            user_msg.index("【话术要求】"), user_msg.index("【补充要求】"),
        )

    def test_extra_prompt_absent_by_default(self) -> None:
        """未携带 extra_prompt 时不产生块，审计记录 null。"""
        svc, mock_agent = _make_svc("话术")
        asyncio.run(svc.generate(_req(), trace_id="tid"))
        user_msg = mock_agent.last_input["messages"][0]["content"]
        self.assertNotIn("【补充要求】", user_msg)
        self.assertIsNone(svc._audit.docs[0]["input"]["extra_prompt"])

    def test_audit_records_extra_prompt(self) -> None:
        svc, _ = _make_svc("话术")
        asyncio.run(svc.generate(
            _req(extra_prompt="突出秋冬新品，提醒会员双倍积分"), trace_id="tid",
        ))
        doc = svc._audit.docs[0]
        self.assertEqual(
            doc["input"]["extra_prompt"], "突出秋冬新品，提醒会员双倍积分",
        )

    def test_user_msg_six_block_order(self) -> None:
        """六块顺序：顾客→商品→活动→券→要求→补充（2.2 / D8）。"""
        svc, mock_agent = _make_svc("话术")
        asyncio.run(svc.generate(_req(
            promotions=[{"promo_id": "p1", "name": "A", "copy": "活动文案"}],
            coupon_names=["生日券"],
            extra_prompt="突出新品",
        ), trace_id="tid"))
        user_msg = mock_agent.last_input["messages"][0]["content"]
        order = (
            "【顾客信息】", "【商品信息】", "【促销活动】",
            "【顾客优惠券】", "【话术要求】", "【补充要求】",
        )
        indexes = [user_msg.index(block) for block in order]
        self.assertEqual(indexes, sorted(indexes))

    def test_default_request_no_new_blocks(self) -> None:
        """未携带新字段：不新增块、无分享方式行（缺省零变化）。"""
        svc, mock_agent = _make_svc("话术")
        asyncio.run(svc.generate(_req(), trace_id="tid"))
        user_msg = mock_agent.last_input["messages"][0]["content"]
        for block in ("【促销活动】", "【顾客优惠券】"):
            self.assertNotIn(block, user_msg)
        self.assertNotIn("分享方式", user_msg)

    def test_audit_records_new_inputs(self) -> None:
        """审计 input 记录新入参（2.3）：活动为 id/name/copy 结构，券为清理后值。"""
        svc, _ = _make_svc("话术")
        asyncio.run(svc.generate(_req(
            promotions=[{
                "promo_id": "mixian", "name": "万象城1000-200",
                "copy": "满1000减200",
            }],
            coupon_names=[" 500元生日券 "],
            customer={
                "union_id": "U_abc", "nickname": "王女士",
                "member_level": "金卡会员", "points": 1260,
            },
        ), trace_id="tid"))
        doc = svc._audit.docs[0]
        self.assertEqual(doc["input"]["promotions"], [
            {"promo_id": "mixian", "name": "万象城1000-200", "copy": "满1000减200"},
        ])
        self.assertEqual(doc["input"]["coupon_names"], ["500元生日券"])
        self.assertEqual(doc["input"]["customer"]["member_level"], "金卡会员")
        self.assertEqual(doc["input"]["customer"]["points"], 1260)

    def test_audit_new_inputs_null_when_absent(self) -> None:
        """未携带新字段：记 null，customer 快照不含会员等级/积分。"""
        svc, _ = _make_svc("话术")
        asyncio.run(svc.generate(_req(), trace_id="tid"))
        doc = svc._audit.docs[0]
        self.assertNotIn("share_mode", doc["input"])
        self.assertNotIn("channel", doc["input"])
        self.assertIsNone(doc["input"]["promotions"])
        self.assertIsNone(doc["input"]["coupon_names"])
        self.assertNotIn("member_level", doc["input"]["customer"])
        self.assertNotIn("points", doc["input"]["customer"])

    def test_audit_promotions_empty_list_null(self) -> None:
        svc, _ = _make_svc("话术")
        asyncio.run(svc.generate(_req(promotions=[]), trace_id="tid"))
        self.assertIsNone(svc._audit.docs[0]["input"]["promotions"])

    def test_audit_coupon_names_explicit_empty_stays_empty(self) -> None:
        """显式 [] 与缺省语义分离：审计区分记录。"""
        svc, _ = _make_svc("话术")
        asyncio.run(svc.generate(_req(coupon_names=[]), trace_id="tid"))
        self.assertEqual(svc._audit.docs[0]["input"]["coupon_names"], [])


class RemovedFieldsCompatibilityTest(unittest.TestCase):
    """携带全套旧字段的请求成功且提示词/审计无残留（契约收敛兼容用例）。"""

    def test_full_legacy_payload_success_no_residue(self) -> None:
        svc, mock_agent = _make_svc("王女士，这件卫衣很合适~")
        legacy = {
            "app_id": "micro_guide",
            "guide_num": "G001",
            "customer": {
                "union_id": "U_abc", "nickname": "王女士",
                "size_info": "M码", "style_preference": "复古运动",
                "scene": "秋季通勤", "budget": "500-800元", "notes": "偏好红色",
            },
            "products": [{
                "title": "FILA 经典卫衣", "color": "米白",
                "sku_id": "U2D240211", "price": 399, "category": "卫衣",
                "material": "纯棉", "selling_points": "重磅面料",
            }],
            "share_mode": "set", "channel": "wechat",
            "pitch_style": "warm", "max_length": 120,
        }
        req = SalesPitchRequest(**legacy)  # 被移除字段被忽略，不报 422
        out = asyncio.run(svc.generate(req, trace_id="tid"))
        self.assertNotIn("error", out)
        user_msg = mock_agent.last_input["messages"][0]["content"]
        for gone in ("分享方式", "渠道", "货号", "价格", "类目", "材质", "卖点",
                     "风格偏好", "使用场景", "尺码", "预算", "备注"):
            self.assertNotIn(gone, user_msg)
        for gone in ("U2D240211", "399", "纯棉", "重磅面料", "复古运动", "500-800元"):
            self.assertNotIn(gone, user_msg)
        doc = svc._audit.docs[0]
        self.assertNotIn("share_mode", doc["input"])
        self.assertNotIn("channel", doc["input"])
        for gone in ("size_info", "style_preference", "scene", "budget", "notes"):
            self.assertNotIn(gone, doc["input"]["customer"])
        for gone in ("sku_id", "price", "category", "material", "selling_points"):
            self.assertNotIn(gone, doc["input"]["products"][0])


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
