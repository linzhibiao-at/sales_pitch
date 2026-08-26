"""query_keywords 按 role 拆分 category 的测试。

bug：intent.category（如裤子品类 梭织长裤）被拼进 _shared_intent_tokens，
导致每个 role 的 keyword 都带裤子品类——鞋的 keyword 变成 "鞋，…，梭织长裤，针织长裤"，
污染文本向量 embedding。修复：category 从 shared 移出，按 role 从
target_slots[role].positive.category 取。
"""

from __future__ import annotations

import unittest

from backend.models import UserIntent
from backend.query_keywords import extract_query_keywords


def _intent(**kw) -> UserIntent:
    base = dict(
        anchor_role="top",
        target_roles=["bottoms", "shoes"],
        gender="女",
        season=["秋"],
        category=["梭织长裤", "针织长裤"],
    )
    base.update(kw)
    return UserIntent(**base)


class QueryKeywordsPerRoleCategoryTest(unittest.TestCase):
    def test_global_category_not_leaked_to_any_role_keyword(self):
        """顶层 intent.category 不属于任何 role 的 keyword（per-role category 才是来源）。
        鞋被裤子品类污染是 bug；bottoms 的品类召回由 role_filter + category_l2 pairing 兜底。"""
        i = _intent()  # intent.category 是裤子品类（顶层，非 per-role）
        kws = extract_query_keywords(i)
        self.assertEqual(len(kws), 2)
        bottoms_kw, shoes_kw = kws
        # 顶层 category 不泄漏到任何 role 的 keyword
        self.assertNotIn("梭织长裤", shoes_kw)
        self.assertNotIn("梭织长裤", bottoms_kw)
        self.assertNotIn("针织长裤", shoes_kw)

    def test_per_role_category_used(self):
        i = _intent(target_slots={
            "bottoms": {"positive": {"category": ["梭织长裤"]}, "negative": {}},
            "shoes": {"positive": {"category": ["运动鞋"]}, "negative": {}},
        })
        kws = extract_query_keywords(i)
        bottoms_kw, shoes_kw = kws
        self.assertIn("梭织长裤", bottoms_kw)
        self.assertIn("运动鞋", shoes_kw)
        self.assertNotIn("梭织长裤", shoes_kw)

    def test_no_category_still_has_role(self):
        i = UserIntent(anchor_role="top", target_roles=["bottoms", "shoes"],
                       gender="女", season=["秋"])
        kws = extract_query_keywords(i)
        self.assertEqual(len(kws), 2)
        self.assertTrue(kws[0].startswith("下装"))
        self.assertTrue(kws[1].startswith("鞋"))

    def test_role_default_category_and_color_added(self):
        """无 per-role category 时按 role 默认品类词补（bottoms→长裤）；color/color_series
        也加入 keyword（text-vector 索引已把 color/season 编入嵌入文本，故 color 能 boost sim）。"""
        # 这次请求：intent.category 是锚点品类(短袖编织衫)，per-role 无 category 但有 color
        i = UserIntent(anchor_role="top", target_roles=["bottoms", "shoes"],
                       gender="女", season=["秋"], category=["短袖编织衫"],
                       target_slots={
                           "bottoms": {"positive": {"color": ["粉色"], "color_series": ["粉色系"]}, "negative": {}},
                           "shoes": {"positive": {"color": ["白色"], "color_series": ["白色系"]}, "negative": {}},
                       })
        kws = extract_query_keywords(i)
        bottoms_kw, shoes_kw = kws
        # bottoms: 默认品类"长裤" + 用户色"粉色"/"粉色系"，不含锚点品类"短袖编织衫"
        self.assertIn("长裤", bottoms_kw)
        self.assertIn("粉色", bottoms_kw)
        self.assertIn("粉色系", bottoms_kw)
        self.assertNotIn("短袖编织衫", bottoms_kw)
        # shoes: 默认"鞋" + 白色/白色系，不含粉色
        self.assertIn("白色", shoes_kw)
        self.assertNotIn("粉色", shoes_kw)


if __name__ == "__main__":
    unittest.main()
