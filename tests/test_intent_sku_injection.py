"""意图解析多输入（SKU/文本/图片）矩阵化重构的单元测试。

验证：
- _build_sku_attr_block：格式化【锚点商品属性】注入块，空 row 返回空串。
- 锚点选举：anchor_source / image_role 在 SKU×图片 同款/异款/单边组合下的取值。
- 纯 SKU 输入短路：不调用 LLM，method=sku_only。
- style_ref 异款图：image_slots 不含 gender/season/anchor_role，避免覆盖 SKU 权威值。
- extract_intent_json：把锚点属性块与图角色提示注入 LLM messages。
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from backend import llm_client
from backend.intent import intent_engine
from backend.intent.intent_engine import (
    IntentResult,
    _build_sku_attr_block,
    extract_intent,
)


def _sku_row(sku_id: str = "ABC123", **kw) -> dict:
    base = {
        "sku_id": sku_id,
        "gender": "男童",
        "age": "中大童",
        "season": ["秋", "冬"],
        "role": "top",
        "category_l2": "单层冲锋衣",
        "color": "军绿",
        "length_class": "long",
        "coverage": "upper",
        "scene_domain": "outdoor",
    }
    base.update(kw)
    return base


class BuildSkuAttrBlockTest(unittest.TestCase):
    def test_empty_row_returns_empty(self):
        self.assertEqual(_build_sku_attr_block({}), "")
        self.assertEqual(_build_sku_attr_block(None), "")

    def test_formats_authoritative_attrs(self):
        block = _build_sku_attr_block(_sku_row())
        self.assertIn("【锚点商品属性】", block)
        self.assertIn("gender=男童", block)
        self.assertIn("anchor_role=上装", block)  # role=top 归一为中文
        self.assertIn("category=单层冲锋衣", block)
        self.assertIn("length_class=long", block)
        self.assertIn("scene_domain=outdoor", block)

    def test_missing_optional_attrs_omitted(self):
        block = _build_sku_attr_block({"sku_id": "X", "gender": "男"})
        self.assertIn("gender=男", block)
        self.assertNotIn("length_class", block)


class AnchorElectionTest(unittest.TestCase):
    """通过 mock extract_intent_json 返回空，隔离 LLM，专测锚点选举与短路。"""

    def _patch_llm(self):
        # 局部 import，extract_intent 内 from backend.llm_client import extract_intent_json
        return mock.patch.object(llm_client, "extract_intent_json", return_value={})

    def test_pure_sku_short_circuits_llm(self):
        with self._patch_llm() as m:
            res = extract_intent("", sku_input_row=_sku_row())
        self.assertEqual(res.method, "sku_only")
        self.assertEqual(res.anchor_source, "sku")
        self.assertEqual(res.image_role, "none")
        self.assertFalse(m.called, "纯 SKU 输入不应调用 LLM")

    def test_sku_plus_image_same_sku_is_anchor(self):
        sku = _sku_row("ABC123")
        img_row = _sku_row("ABC123")  # 图搜 top1 与输入同款
        with self._patch_llm():
            res = extract_intent(
                "配一下", image_base64="x", image_anchor_row=img_row,
                image_similarity=0.95, sku_input_row=sku,
            )
        self.assertEqual(res.anchor_source, "sku")
        self.assertEqual(res.image_role, "anchor")

    def test_sku_plus_image_different_sku_is_style_ref(self):
        sku = _sku_row("ABC123")
        img_row = _sku_row("XYZ789")  # 图搜 top1 与输入异款
        with self._patch_llm():
            res = extract_intent(
                "配一下", image_base64="x", image_anchor_row=img_row,
                image_similarity=0.95, sku_input_row=sku,
            )
        self.assertEqual(res.anchor_source, "sku")
        self.assertEqual(res.image_role, "style_ref")

    def test_image_only_anchor_is_image(self):
        img_row = _sku_row("XYZ789")
        with self._patch_llm():
            res = extract_intent(
                "", image_base64="x", image_anchor_row=img_row,
                image_similarity=0.95,
            )
        self.assertEqual(res.anchor_source, "image")
        self.assertEqual(res.image_role, "anchor")


class StyleRefStripTest(unittest.TestCase):
    """image_role=style_ref 时，图搜 slots 不得贡献优先字段。"""

    def test_style_ref_strips_priority_slots(self):
        sku = _sku_row("ABC123")
        img_row = _sku_row("XYZ789", gender="女")  # 异款，gender 与 sku 冲突
        with mock.patch.object(llm_client, "extract_intent_json", return_value={}):
            res = extract_intent(
                "配一下", image_base64="x", image_anchor_row=img_row,
                image_similarity=0.95, sku_input_row=sku,
            )
        image_src = res.source_slots.get("image", {})
        self.assertNotIn("gender", image_src, "异款风格参考图不得贡献 gender")
        self.assertNotIn("season", image_src)
        self.assertNotIn("anchor_role", image_src)

    def test_anchor_keeps_priority_slots(self):
        img_row = _sku_row("XYZ789")
        with mock.patch.object(llm_client, "extract_intent_json", return_value={}):
            res = extract_intent(
                "", image_base64="x", image_anchor_row=img_row,
                image_similarity=0.95,
            )
        image_src = res.source_slots.get("image", {})
        self.assertIn("gender", image_src)


class LlmInjectionTest(unittest.TestCase):
    """extract_intent_json 把锚点属性块与图角色注入 messages。"""

    def _capture(self):
        calls: list[dict] = []
        # 返回合法意图 JSON，模拟主模型正常返回
        ok = json.dumps({"gender": "男", "season": ["夏"], "category": ["短袖T恤"]})

        def fake_chat_block(section, messages, **kw):
            calls.append({"section": section, "messages": messages})
            return ok

        return calls, fake_chat_block

    def test_injects_attr_block_and_image_url(self):
        calls, fake = self._capture()
        with mock.patch.object(llm_client, "_chat_block", side_effect=fake):
            llm_client.extract_intent_json(
                "配一条裤子",
                image_base64="aGVsbG8=",  # 任意非空
                anchor_attr_text="【锚点商品属性】gender=男童",
                image_role="style_ref",
            )
        self.assertTrue(calls, "_chat_block 应被调用")
        msgs = calls[0]["messages"]
        user = msgs[-1]["content"]
        self.assertIsInstance(user, list)
        texts = [b["text"] for b in user if b.get("type") == "text"]
        joined = "\n".join(texts)
        self.assertIn("【锚点商品属性】", joined)
        self.assertIn("【图的角色】style_ref", joined)
        self.assertIn("【用户文字】", joined)
        self.assertTrue(any(b.get("type") == "image_url" for b in user))

    def test_no_attr_block_text_only(self):
        calls, fake = self._capture()
        with mock.patch.object(llm_client, "_chat_block", side_effect=fake):
            llm_client.extract_intent_json("只要文字", image_base64=None)
        user = calls[0]["messages"][-1]["content"]
        self.assertIsInstance(user, str)
        self.assertNotIn("【锚点商品属性】", user)
        self.assertIn("只要文字", user)

    def test_attr_block_without_image(self):
        calls, fake = self._capture()
        with mock.patch.object(llm_client, "_chat_block", side_effect=fake):
            llm_client.extract_intent_json(
                "配裤子", image_base64=None,
                anchor_attr_text="【锚点商品属性】gender=男童",
                image_role="none",
            )
        user = calls[0]["messages"][-1]["content"]
        self.assertIsInstance(user, str)
        self.assertIn("【锚点商品属性】", user)
        self.assertNotIn("【图的角色】", user)


if __name__ == "__main__":
    unittest.main()
