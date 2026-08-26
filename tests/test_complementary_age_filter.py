"""complementary 召回的 age 粗排下推测试。

背景：complementary 召回（fila_sku_complementary_vectors）此前只下推
role/gender/attr/scene_domain/season，**漏了 age**，导致童装锚点能召回到
异段童装（如中大童底装召回小童上装）。本测试锁定 age expr 下推。
"""

from __future__ import annotations

import unittest

from backend.models import UserIntent
from backend.services.complementary_recall import _build_role_milvus_expr


def _intent() -> UserIntent:
    return UserIntent(anchor_role="bottoms", target_roles=["top"])


class ComplementaryAgeExprTest(unittest.TestCase):
    def test_concrete_segment_anchor_emits_age_in(self):
        # 中大童底装锚点 → 候选上装必须 中大童 或 通码（排除小童/婴幼童/成人款）
        anchor = {"role": "bottoms", "age": "中大童"}
        expr = _build_role_milvus_expr(_intent(), "top", anchor_row=anchor)
        self.assertIn('age in ["中大童", "通码"]', expr)

    def test_small_kids_anchor(self):
        anchor = {"role": "bottoms", "age": "小童"}
        expr = _build_role_milvus_expr(_intent(), "top", anchor_row=anchor)
        self.assertIn('age in ["小童", "通码"]', expr)

    def test_tongma_anchor_excludes_adult_only(self):
        # 通码锚点覆盖全段，可与任一童装段搭配，但仍排除成人款（age 为空）
        anchor = {"role": "bottoms", "age": "通码"}
        expr = _build_role_milvus_expr(_intent(), "top", anchor_row=anchor)
        self.assertIn('age != ""', expr)
        self.assertNotIn("age in [", expr)

    def test_adult_anchor_no_age_filter(self):
        # 成人款锚点（age 空）不应加 age 约束
        anchor = {"role": "bottoms", "age": ""}
        expr = _build_role_milvus_expr(_intent(), "top", anchor_row=anchor)
        self.assertNotIn("age in [", expr)
        self.assertNotIn('age != ""', expr)

    def test_anchor_age_falls_back_to_intent_age(self):
        # 锚点 age 缺失但 intent 有 age（如 backfill 自锚点）时仍下推
        intent = UserIntent(anchor_role="bottoms", target_roles=["top"], age="中大童")
        anchor = {"role": "bottoms", "age": ""}
        expr = _build_role_milvus_expr(intent, "top", anchor_row=anchor)
        self.assertIn('age in ["中大童", "通码"]', expr)

    def test_no_anchor_no_age_filter(self):
        expr = _build_role_milvus_expr(_intent(), "top", anchor_row=None)
        self.assertNotIn("age in [", expr)
        self.assertNotIn('age != ""', expr)


if __name__ == "__main__":
    unittest.main()
