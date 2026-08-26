"""泳装（泳衣/泳裤/连体泳衣/开衫泳衣/短裤泳装）召回治理单元测试。

背景：泳装做"同伴"时被三重规则挡在召回结果外（仅做锚点能出现）：
  1. length_class 季节冲突误杀：开衫泳衣→long（标题含"开衫"）、短裤泳装→short，
     被「长袖上装×短款下装季节冲突」reject。但沙滩域里长款防晒开衫×短款泳裤
     是正确搭配，length 不应作 season 代理。
  2. is_intimate 常驻过滤：泳裤/泳衣/连体泳衣/开衫泳衣 判 is_intimate=True，
     被 build_attr_milvus_expr/build_attr_es_filter 常驻 is_intimate==false 排除，
     永远做不了同伴。设计本意是排除内裤/文胸，泳装不应同等对待。

修复后预期：
  - 泳装 cat2/标题 → length_class 归 n/a（退出季节冲突规则）
  - 泳裤/泳衣/连体泳衣/开衫泳衣 → is_intimate=False（可做同伴）
  - 内裤/文胸/运动内衣 仍 is_intimate=True（回归保护）
  - 开衫泳衣(anchor) × 短裤泳装(companion) 不再判冲突
"""

from __future__ import annotations

import unittest

from backend.intent.sku_attributes import (
    extract_is_intimate,
    extract_length_class,
    get_attr,
    is_swimwear,
)
from backend.ranking.outfit_conflict import (
    build_attr_es_filter,
    build_attr_milvus_expr,
    check_companion_conflict,
)


class TestSwimwearLengthClass(unittest.TestCase):
    """泳装 length_class 归 n/a，退出「长袖×短款下装季节冲突」。"""

    def test_swim_top_kaishan_yongyi_is_na(self):
        """K12G423501FZA 开衫泳衣：role=top, cat2=泳装。原 long（标题含"开衫"），修复后 n/a。"""
        self.assertEqual(
            extract_length_class("top", "泳装", "女中大童抗紫外线开衫泳衣"), "n/a"
        )

    def test_swim_bottoms_short_yongzhuang_is_na(self):
        """K12G423608FZA 短裤泳装：role=bottoms, cat2=泳装。原 short，修复后 n/a。"""
        self.assertEqual(
            extract_length_class("bottoms", "泳装", "女中大童短裤泳装"), "n/a"
        )

    def test_swim_cats_all_na(self):
        """泳装各 cat2 无论 role 都归 n/a。"""
        for cat2 in ("连体泳衣", "分体泳衣", "儿童连体泳衣", "泳衣", "开衫泳衣", "泳裤"):
            self.assertEqual(extract_length_class("top", cat2, ""), "n/a", msg=cat2)
            self.assertEqual(extract_length_class("bottoms", cat2, ""), "n/a", msg=cat2)

    def test_non_swim_long_top_unaffected(self):
        """回归：普通长袖上装仍 long（开衫 keyword 对非泳装仍生效）。"""
        self.assertEqual(extract_length_class("top", "编织开衫", "女士开衫"), "long")
        self.assertEqual(extract_length_class("top", "长袖T", "长袖T恤"), "long")

    def test_non_swim_short_bottoms_unaffected(self):
        """回归：普通短裤下装仍 short。"""
        self.assertEqual(extract_length_class("bottoms", "梭织短裤", "短裤"), "short")

    def test_generic_top_cat_length_defaults(self):
        """泛称上/下装中类按业务定长短款（标题无长短信号时由 cat_l2 兜底）。

        - top 服类/外套类 → long；短T类/内衣类/内搭类/内搭 → short
        - bottoms 针织裤 → long；滑雪裤 → short
        覆盖原 n/a 漏判的 ~115 个 top/bottoms SKU。
        """
        for cat2 in ("防晒服", "滑雪服", "棒球服", "外套类"):
            self.assertEqual(
                extract_length_class("top", cat2, ""), "long", msg=cat2
            )
        for cat2 in ("短T类", "内衣类", "内搭类", "内搭"):
            self.assertEqual(
                extract_length_class("top", cat2, ""), "short", msg=cat2
            )
        self.assertEqual(extract_length_class("bottoms", "针织裤", ""), "long")
        self.assertEqual(extract_length_class("bottoms", "滑雪裤", ""), "long")


class TestSwimwearIsIntimate(unittest.TestCase):
    """泳装不再判 is_intimate=True，可做同伴推荐。"""

    def test_swim_cats_not_intimate(self):
        for cat2 in ("泳裤", "泳衣", "连体泳衣", "分体泳衣", "开衫泳衣", "儿童连体泳衣"):
            self.assertFalse(extract_is_intimate(cat2, ""), msg=cat2)

    def test_swim_title_keywords_not_intimate(self):
        """标题含 泳衣/泳裤/泳装 也不再判 intimate。"""
        self.assertFalse(extract_is_intimate("泳装", "男士泳裤吸湿速干"))
        self.assertFalse(extract_is_intimate("泳装", "抗紫外线开衫泳衣"))
        self.assertFalse(extract_is_intimate("连体泳衣", "缪斯腰连体泳衣"))

    def test_underwear_still_intimate(self):
        """回归：内裤/文胸/运动内衣 仍 is_intimate=True。"""
        self.assertTrue(extract_is_intimate("内裤", ""))
        self.assertTrue(extract_is_intimate("运动内衣", ""))
        self.assertTrue(extract_is_intimate("运动BRA", ""))
        self.assertTrue(extract_is_intimate("", "男士内裤"))
        self.assertTrue(extract_is_intimate("", "运动内衣"))


class TestSwimwearConflictEndToEnd(unittest.TestCase):
    """端到端：开衫泳衣 × 短裤泳装 不再被季节冲突规则 reject。"""

    def test_swim_top_and_swim_bottom_no_conflict(self):
        anchor = {
            "sku_id": "K12G423501FZA",
            "role": "top",
            "category_l2": "泳装",
            "title": "女中大童抗紫外线开衫泳衣",
            "scene_domain": "swim",
        }
        companion = {
            "sku_id": "K12G423608FZA",
            "role": "bottoms",
            "category_l2": "泳装",
            "title": "女中大童短裤泳装",
            "scene_domain": "swim",
        }
        # 修复前：长袖上装×短款下装季节冲突 → True；修复后：泳装 length=n/a → False
        self.assertFalse(check_companion_conflict(anchor, companion))

    def test_swim_anchor_length_exclusion_dropped(self):
        """泳装锚点 length_class=n/a，build_attr_milvus_expr 不再下推 length_class != "short"。

        is_intimate==false 常驻仍在（仅泳装本身不再 intimate，但内裤等仍排除）。
        """
        anchor = {
            "role": "top",
            "category_l2": "泳装",
            "title": "开衫泳衣",
            "scene_domain": "swim",
        }
        expr = build_attr_milvus_expr(anchor, "bottoms") or ""
        self.assertNotIn('length_class != "short"', expr)
        self.assertIn('is_intimate == "false"', expr)  # 常驻保留

        es = build_attr_es_filter(anchor, "bottoms") or {}
        terms = es.get("must_not", [])
        self.assertNotIn({"term": {"length_class": "short"}}, terms)
        self.assertIn({"term": {"is_intimate": True}}, terms)  # 常驻保留

    def test_swim_companion_not_intimate_filtered(self):
        """泳装同伴 is_intimate=False，不会被 is_intimate 常驻过滤挡掉。"""
        companion = {
            "role": "bottoms",
            "category_l2": "泳装",
            "title": "短裤泳装",
            "scene_domain": "swim",
        }
        self.assertEqual(get_attr(companion, "is_intimate"), "False")
        self.assertEqual(get_attr(companion, "length_class"), "n/a")


class TestSwimwearVlmBackfillInvariant(unittest.TestCase):
    """ETL 全链路：VLM 回退不得把泳装 length_class 从 n/a 改回 long/short。

    防回归：即便 sku_length_vlm.csv 里残留了泳装的 VLM 判定值（旧 CSV 或人工
    误跑），resolve_length_class 仍须返回 n/a，否则季节冲突规则会重新误杀
    开衫泳衣×短裤泳装。
    """

    def setUp(self):
        from scripts import etl_common
        self._etl = etl_common
        # 模拟 VLM CSV 里泳装被判 long（视觉上开衫确实长款）
        self._etl.load_length_class_vlm_index.cache_clear()
        self._orig = self._etl.load_length_class_vlm_index
        self._etl.load_length_class_vlm_index = lambda: {  # noqa: E731
            "K12G423501FZA": "long",
            "K12G423608FZA": "short",
        }

    def tearDown(self):
        self._etl.load_length_class_vlm_index = self._orig
        self._etl.load_length_class_vlm_index.cache_clear()

    def test_swim_not_overridden_by_vlm(self):
        from scripts.etl_common import resolve_length_class
        self.assertEqual(
            resolve_length_class("top", "泳装", "开衫泳衣", "K12G423501FZA"), "n/a"
        )
        self.assertEqual(
            resolve_length_class("bottoms", "泳装", "短裤泳装", "K12G423608FZA"), "n/a"
        )

    def test_non_swim_na_still_uses_vlm(self):
        """回归：非泳装 n/a 下装仍正常走 VLM 回退。

        用「裙装/某裙」：下装三段规则都不命中（无短/五分/七分/半裙/半身裙，
        非裤裙/背带类）→ n/a → VLM（mock K12G423608FZA→short）。
        """
        from scripts.etl_common import resolve_length_class
        self.assertEqual(
            resolve_length_class("bottoms", "裙装", "某裙", "K12G423608FZA"), "short"
        )


if __name__ == "__main__":
    unittest.main()
