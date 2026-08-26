"""长袖上装图锚点 × 短款下装 预过滤回归测试。

复现 bug：上传长袖上装图片但图搜 top1 sim < 0.9（无高置信匹配 SKU）时，
意图模块走虚拟图锚点分支。重构前虚拟锚点丢失 ``category_l2``，
``get_attr(anchor, "length_class")`` 退化为 ``"n/a"``，导致
``build_attr_es_filter`` / ``build_attr_milvus_expr`` / ``check_companion_conflict``
三处 length_class 预过滤全部失效，五分裤（``length_class=short``）被召回。

重构后意图模块统一融合产出锚点结构化属性，虚拟锚点也带 ``category_l2`` →
``length_class=long``，三处预过滤恢复生效。
"""

from __future__ import annotations

import unittest

from backend.intent.intent_engine import resolve_anchor_attrs
from backend.intent.sku_attributes import get_attr
from backend.models import UserIntent
from backend.ranking.outfit_conflict import (
    build_attr_es_filter,
    build_attr_milvus_expr,
    build_scene_domain_es_filter,
    check_companion_conflict,
)
from backend.services.recommend_service import build_upload_anchor_row


def _long_sleeve_intent() -> UserIntent:
    """LLM 直判上传图为「单层冲锋衣」（长袖上装）；文本为空、图搜投票关闭。"""
    return UserIntent(
        query_type="item_to_outfit",
        anchor_role="top",
        target_roles=["bottoms", "shoes"],
        gender="男童",
        season=["秋", "冬"],
        category=["单层冲锋衣"],
    )


def _short_pants_companion() -> dict:
    """K12B533832FGN 等五分裤：role=bottoms, length_class=short。"""
    return {
        "sku_id": "K12B533832FGN",
        "role": "bottoms",
        "category_l2": "梭织五分裤",
        "title": "男中大童户外系列梭织五分裤",
        "gender": ["男童"],
        "season": ["秋"],
        "length_class": "short",
        "coverage": "lower",
        "layer": "n/a",
        "is_intimate": False,
        "scene_domain": "outdoor",
    }


class ResolveAnchorAttrsVirtualImageTest(unittest.TestCase):
    """意图模块对虚拟图锚点（无高 sim 匹配 SKU）融合产出属性。"""

    def test_virtual_long_sleeve_anchor_gets_length_class_long(self):
        # 无 image_anchor_row（sim 0.61 < 0.9）→ 虚拟图锚点
        attrs = resolve_anchor_attrs(
            _long_sleeve_intent(),
            image_anchor_row=None,
            image_similarity=0.61,
            sim_threshold=0.9,
        )
        self.assertEqual(attrs["role"], "top")
        self.assertEqual(attrs["category_l2"], "单层冲锋衣")
        self.assertEqual(attrs["length_class"], "long")

    def test_virtual_anchor_attrs_drive_attr_filters(self):
        attrs = resolve_anchor_attrs(
            _long_sleeve_intent(),
            image_anchor_row=None,
            image_similarity=0.61,
            sim_threshold=0.9,
        )
        # ES must_not 应排除 length_class=short
        es_filter = build_attr_es_filter(attrs, "bottoms")
        self.assertIsNotNone(es_filter)
        must_not = es_filter["must_not"]
        self.assertIn({"term": {"length_class": "short"}}, must_not)

        # Milvus expr 应含 length_class != "short"
        milvus_expr = build_attr_milvus_expr(attrs, "bottoms")
        self.assertIsNotNone(milvus_expr)
        self.assertIn('length_class != "short"', milvus_expr)

        # 安全网：长袖×短款下装季节冲突规则 YAML gate gender=["男"]。
        # 男童锚点 normalize_genders={"男童"} 不命中 ["男"] → 安全网不拦（gender 守卫
        # 修复后规则真正按 gender gate，不再 over-broad 对男童也生效）；
        # 但 build_attr_* 下推（gender 无关，role=top+long → 排 short）仍拦截五分裤。
        attrs_boy = dict(attrs)
        self.assertFalse(check_companion_conflict(attrs_boy, _short_pants_companion()))
        # 成人男锚点命中 ["男"] → 安全网拦截
        attrs_man = dict(attrs); attrs_man["gender"] = ["男"]
        self.assertTrue(check_companion_conflict(attrs_man, _short_pants_companion()))

    def test_virtual_short_sleeve_anchor_does_not_exclude_short_bottoms(self):
        """短袖上装虚拟锚点不应误伤短款下装（避免过度过滤）。"""
        intent = UserIntent(
            anchor_role="top",
            category=["短袖T"],
            target_roles=["bottoms", "shoes"],
        )
        attrs = resolve_anchor_attrs(
            intent, image_anchor_row=None, image_similarity=0.6, sim_threshold=0.9,
        )
        self.assertEqual(attrs["length_class"], "short")
        es_filter = build_attr_es_filter(attrs, "bottoms")
        if es_filter:  # 短袖上装不应触发 length_class=short 排除
            self.assertNotIn({"term": {"length_class": "short"}}, es_filter["must_not"])
        # 短袖T 锚点已兜底 daily；用 daily 短裤避免引入 daily×outdoor 场景冲突，
        # 聚焦验证 length_class 不误伤（短袖×短裤无 long×short 规则）
        daily_short_pants = dict(_short_pants_companion())
        daily_short_pants["scene_domain"] = "daily"
        self.assertFalse(check_companion_conflict(attrs, daily_short_pants))


class ResolveAnchorAttrsRealSkuTest(unittest.TestCase):
    """高 sim 真实 SKU 锚点：继承 SKU 已持久化属性（保留 VLM 回补精度）。"""

    def test_high_sim_real_sku_inherits_persisted_attrs(self):
        sku = {
            "sku_id": "A11M637704FBK",
            "role": "top",
            "category_l2": "单层冲锋衣",
            "title": "男童单层冲锋衣",
            "length_class": "long",  # VLM 回补值
            "coverage": "upper",
            "layer": "outer",
            "is_intimate": False,
            "scene_domain": "outdoor",
        }
        attrs = resolve_anchor_attrs(
            _long_sleeve_intent(),
            image_anchor_row=sku,
            image_similarity=0.95,
            sim_threshold=0.9,
        )
        self.assertEqual(attrs["length_class"], "long")
        self.assertEqual(attrs["layer"], "outer")  # 继承而非重派生
        self.assertEqual(attrs["category_l2"], "单层冲锋衣")


class GetAttrFallbackTest(unittest.TestCase):
    """get_attr 对虚拟锚点仍能用融合属性而非退化为 n/a。"""

    def test_get_attr_reads_fused_length_class(self):
        attrs = resolve_anchor_attrs(
            _long_sleeve_intent(),
            image_anchor_row=None,
            image_similarity=0.61,
            sim_threshold=0.9,
        )
        self.assertEqual(get_attr(attrs, "length_class"), "long")
        self.assertNotEqual(get_attr(attrs, "length_class"), "n/a")


class BuildUploadAnchorRowIntegrationTest(unittest.TestCase):
    """端到端：recommend_service 构造的虚拟图锚点驱动 length_class 预过滤。

    复现 bug 场景：上传长袖图，图搜 top1 sim=0.61 < 0.9（image_anchor_row=None）。
    """

    def _virtual_anchor(self) -> dict:
        attrs = resolve_anchor_attrs(
            _long_sleeve_intent(),
            image_anchor_row=None,
            image_similarity=0.61,
            sim_threshold=0.9,
        )
        return build_upload_anchor_row(
            anchor_attrs=attrs,
            image_anchor_row=None,
            sku_anchor_sim=0.61,
            image_base64="AAAA",
            trace_id="3af55377ab6f44fbb90edf5eb2065d14",
        )

    def test_virtual_anchor_carries_fused_attrs(self):
        anchor = self._virtual_anchor()
        self.assertTrue(anchor["_is_virtual_image_anchor"])
        self.assertTrue(anchor["sku_id"].startswith("img_"))
        self.assertEqual(anchor["category_l2"], "单层冲锋衣")
        self.assertEqual(anchor["length_class"], "long")
        self.assertEqual(anchor["role"], "top")

    def test_virtual_anchor_drives_es_length_class_exclusion(self):
        anchor = self._virtual_anchor()
        es_filter = build_attr_es_filter(anchor, "bottoms")
        self.assertIn({"term": {"length_class": "short"}}, es_filter["must_not"])

    def test_virtual_anchor_rejects_short_pants_via_safety_net(self):
        """安全网 gender gate：男童锚点不命中 YAML gender=["男"]（下推仍保护），
        成人男锚点命中 → 安全网拦截五分裤。"""
        anchor = self._virtual_anchor()
        # 男童锚点（_virtual_anchor 继承 _long_sleeve_intent gender=男童）→ 安全网不拦
        self.assertFalse(check_companion_conflict(anchor, _short_pants_companion()))
        # 成人男锚点 → 安全网拦
        anchor_man = dict(anchor); anchor_man["gender"] = ["男"]
        self.assertTrue(check_companion_conflict(anchor_man, _short_pants_companion()))


def _cycling_pants_companion() -> dict:
    """A11M448602FBK 骑行裤：role=bottoms, length_class=long, scene_domain=cycling。

    长款骑行裤不被 length_class 预过滤拦（非 short），靠 scene_domain 跨营互斥拦截。
    """
    return {
        "sku_id": "A11M448602FBK",
        "role": "bottoms",
        "category_l2": "针织裤",
        "title": "FILA CYCLING男子专业运动专业骑行裤",
        "gender": ["男"],
        "season": ["冬"],
        "length_class": "long",
        "coverage": "lower",
        "layer": "n/a",
        "is_intimate": False,
        "scene_domain": "cycling",
    }


class OutdoorAnchorSceneConflictTest(unittest.TestCase):
    """单层冲锋衣(户外) × 骑行裤(cycling) 跨营冲突回归（A+B）。"""

    def _virtual_outdoor_anchor(self) -> dict:
        attrs = resolve_anchor_attrs(
            _long_sleeve_intent(),
            image_anchor_row=None,
            image_similarity=0.61,
            sim_threshold=0.9,
        )
        return build_upload_anchor_row(
            anchor_attrs=attrs,
            image_anchor_row=None,
            sku_anchor_sim=0.61,
            image_base64="AAAA",
            trace_id="cycling_pants_regression",
        )

    def test_a_virtual_anchor_derives_outdoor_scene(self):
        """A：虚拟冲锋衣锚点 scene_domain 应为 outdoor（不再退化为 ""）。"""
        anchor = self._virtual_outdoor_anchor()
        self.assertEqual(anchor["scene_domain"], "outdoor")

    def test_b_es_filter_only_allows_outdoor_for_outdoor_anchor(self):
        """B1：outdoor 锚点 → scene_domain 正向隔离只允许 outdoor + 中性配件。"""
        anchor = self._virtual_outdoor_anchor()
        scene_filter = build_scene_domain_es_filter(anchor, "bottoms")
        self.assertIsNotNone(scene_filter)
        allow = scene_filter["terms"]["scene_domain"]
        self.assertEqual(sorted(allow), ["", "outdoor"])
        self.assertNotIn("cycling", allow)
        self.assertNotIn("golf", allow)
        self.assertNotIn("tennis", allow)

    def test_b_milvus_expr_only_allows_outdoor_for_outdoor_anchor(self):
        """B1：outdoor 锚点 → Milvus expr 正向隔离为 scene_domain == "outdoor"。"""
        from backend.ranking.outfit_conflict import build_scene_domain_milvus_expr

        anchor = self._virtual_outdoor_anchor()
        expr = build_scene_domain_milvus_expr(anchor, "bottoms")
        self.assertIsNotNone(expr)
        self.assertIn('"outdoor"', expr)
        self.assertNotIn('"cycling"', expr)

    def test_b_safety_net_rejects_cycling_pants(self):
        """B2：check_companion_conflict(冲锋衣, 骑行裤) 应为 True。"""
        anchor = self._virtual_outdoor_anchor()
        self.assertTrue(check_companion_conflict(anchor, _cycling_pants_companion()))

    def test_same_camp_outdoor_pants_not_rejected(self):
        """不过滤：冲锋衣(outdoor) × 户外裤(outdoor) 同营，不应冲突。"""
        anchor = self._virtual_outdoor_anchor()
        outdoor_pants = dict(_cycling_pants_companion())
        outdoor_pants["scene_domain"] = "outdoor"
        outdoor_pants["sku_id"] = "OUTDOOR_PANTS"
        self.assertFalse(check_companion_conflict(anchor, outdoor_pants))

    def test_daily_pants_still_rejected_by_existing_rule(self):
        """既有 daily×sport 规则不变：冲锋衣(outdoor) × 日常裤(daily) 仍冲突。"""
        anchor = self._virtual_outdoor_anchor()
        daily_pants = dict(_cycling_pants_companion())
        daily_pants["scene_domain"] = "daily"
        daily_pants["sku_id"] = "DAILY_PANTS"
        self.assertTrue(check_companion_conflict(anchor, daily_pants))


if __name__ == "__main__":
    unittest.main()
