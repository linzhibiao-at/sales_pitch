"""series per-role 正向下推回归测试（锁定 series wiring 修复）。

背景：用户文案显式把某系列归属到某 target_role（如「搭配粉色 ORIGINALE 系列
裤子」）时，该 series 必须落到 ``target_slots[role].positive.series``，并在
Milvus（``build_role_milvus_expr_parts``）与 ES（``build_role_es_positive_filters``）
两路下推为 ``series == "X"`` 硬过滤——**即使该 role 因有其他 positive（如
category）触发 bypass、跳过锚点同系隔离，per-role 正向 series 仍照常下推**。

此前 prompt §十五/§十七 指示「不要把锚点 series 复制进 positive」，导致当用户
要的 series 恰好等于锚点 series 时，LLM 只把它写到全局 ``intent.series``，
``target_slots.bottoms.positive.series`` 为空 → series 约束从未下推给 bottoms
（同系隔离又被 bypass 跳过）→ 用户显式要的系列被静默丢弃。

本测试只锁代码侧下推通路（prompt 侧由 intent_extract.md §十五/§十七 调整）。
"""

from __future__ import annotations

import unittest

from backend.intent.role_slots import build_role_es_positive_filters, build_role_milvus_expr_parts
from backend.models import UserIntent
from backend.services.outfit_recall import _item_violates_intent


def _intent_bottoms_originale() -> UserIntent:
    """模拟「搭配粉色 ORIGINALE 系列裤子」解析结果：series 落到 bottoms positive，
    同时 bottoms 还有 category positive（触发 bypass）。"""
    return UserIntent(
        series="FILA ORIGINALE",  # 顶层（锚点权威，锚点本身也是 ORIGINALE）
        target_slots={
            "bottoms": {
                "positive": {
                    "color": ["粉色"],
                    "color_series": ["粉色系"],
                    "category": ["梭织长裤", "针织长裤"],
                    "series": "FILA ORIGINALE",  # ← 修复后：显式归属到 bottoms
                },
                "negative": {},
            }
        },
    )


class TestMilvusPerRoleSeriesPushdown(unittest.TestCase):
    def test_series_positive_pushed_under_category_bypass(self):
        intent = _intent_bottoms_originale()
        parts = build_role_milvus_expr_parts(intent, "bottoms")
        joined = " && ".join(parts)
        # per-role series 正向必须下推为 series == "FILA ORIGINALE"
        self.assertIn('series == "FILA ORIGINALE"', joined)
        # category 正向也在（确认 bypass 触发条件仍在，但不影响 series 下推）
        self.assertIn("梭织长裤", joined)


def _intent_bottoms_performance() -> UserIntent:
    """模拟「搭配 PERFORMANCE 系列裤子」解析结果：锚点=HERITAGE 上衣，
    bottoms 显式 series=PERFORMANCE + category positive（触发 bypass）。"""
    return UserIntent(
        series="HERITAGE",  # 顶层=锚点系列
        target_slots={
            "bottoms": {
                "positive": {
                    "category": ["梭织长裤", "针织长裤"],
                    "series": "PERFORMANCE",
                },
                "negative": {},
            }
        },
    )


def _bottoms_row(series: str, category_l2: str = "针织长裤") -> dict:
    """固定搭配库取回的 bottoms SKU 行（带 series/category_l2）。"""
    return {
        "sku_id": "TEST_BOTTOMS",
        "role": "bottoms",
        "series": series,
        "category_l2": category_l2,
        "color_series": [],
        "length_class": "",
        "coverage": "",
        "scene_domain": "",
        "modeling": "",
        "price": 0,
        "age": "",
    }


class TestEsPerRoleSeriesPushdown(unittest.TestCase):
    def test_series_positive_filter_emitted(self):
        intent = _intent_bottoms_originale()
        # ES 通路3 调用：exclude_slots=("color_series","category","modeling")，
        # series 不在排除集 → 必须产出 series term。
        filters = build_role_es_positive_filters(
            intent, "bottoms",
            exclude_slots=("color_series", "category", "modeling"),
        )
        found = any(
            f.get("term", {}).get("series") == "FILA ORIGINALE"
            or f.get("term", {}).get("series", {}).get("value") == "FILA ORIGINALE"
            for f in filters
        )
        self.assertTrue(found, f"series term missing in {filters}")


class TestAnchorGraphSeriesCheck(unittest.TestCase):
    """anchor_graph 通路事后过滤（``_item_violates_intent``）必须读 per-role
    positive.series——否则 bypass 跳过锚点同系隔离后，固定搭配库取回的
    非目标系列裤子（如锚点 HERITAGE 的原配裤）会绕过 series 约束长驱直入。
    镜像 text_vector/query2es/complementary 三路 build_role_*_positive 的下推。"""

    def test_non_matching_series_violates(self):
        """HERITAGE 裤子 vs 用户要 PERFORMANCE → 必须判违反。"""
        intent = _intent_bottoms_performance()
        row = _bottoms_row(series="HERITAGE")
        violated, reason = _item_violates_intent(row, intent, "bottoms")
        self.assertTrue(violated, f"HERITAGE 裤子应被 PERFORMANCE positive 拦下，reason={reason!r}")
        self.assertIn("series", reason)

    def test_matching_series_passes(self):
        """PERFORMANCE 裤子 → 不违反（category 也匹配）。"""
        intent = _intent_bottoms_performance()
        row = _bottoms_row(series="PERFORMANCE")
        violated, _ = _item_violates_intent(row, intent, "bottoms")
        self.assertFalse(violated)

    def test_empty_series_item_passes(self):
        """item series 缺失 → 不据此剔除（与 modeling 缺失放行一致，交其它规则）。"""
        intent = _intent_bottoms_performance()
        row = _bottoms_row(series="")
        violated, _ = _item_violates_intent(row, intent, "bottoms")
        self.assertFalse(violated)


if __name__ == "__main__":
    unittest.main()
