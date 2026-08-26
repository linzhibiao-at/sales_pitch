"""season 跨季兼容矩阵单元测试。

背景：原 season 过滤（Milvus ``season like "%春%"``、ES ``wildcard *春*``、
安全网 ``season_conflict``）严格子串/交集匹配，无跨季兼容。导致春锚点
（如长袖T恤 season=春）把所有夏/秋款下装在粗排与安全网两层全清零，
即便用户显式要的某系列下装（如 ORIGINALE 粉色长裤，库里只有夏/秋款）
也召不出来。

修复：加 ``season_compatible_set``（春夏/秋冬配对：春↔夏、秋↔冬），在
Milvus 粗排、ES 粗排、``season_conflict`` 安全网三处统一用它扩展 want 集。

覆盖：
  - season_compatible_set：配对展开、去重保序、空入参、常青/四季（已归一为四
    季）保持兼容
  - season_conflict：春锚点×夏款=不冲突；冬锚点×夏款=仍冲突（保护跨季初衷）
  - _build_role_milvus_expr：intent.season=[春] → expr 含 season like %春% 与
    season like %夏%
  - _append_season_sku：[春] → wildcard 含 春 与 夏
"""

from __future__ import annotations

import unittest

from backend.models import UserIntent, season_compatible_set
from backend.ranking.scoring import season_conflict
from backend.retrieval.es_intent import _append_season_sku
from backend.services.complementary_recall import _build_role_milvus_expr


class TestSeasonCompatibleSet(unittest.TestCase):
    def test_spring_summer_pair(self):
        # 春↔夏
        self.assertEqual(season_compatible_set(["春"]), ["春", "夏"])
        self.assertEqual(season_compatible_set(["夏"]), ["夏", "春"])

    def test_autumn_winter_pair(self):
        # 秋↔冬
        self.assertEqual(season_compatible_set(["秋"]), ["秋", "冬"])
        self.assertEqual(season_compatible_set(["冬"]), ["冬", "秋"])

    def test_empty_and_unknown(self):
        self.assertEqual(season_compatible_set([]), [])
        self.assertEqual(season_compatible_set([""]), [])

    def test_multi_dedup_preserve_order(self):
        # 春+秋 → 春,夏,秋,冬（去重保序）
        self.assertEqual(season_compatible_set(["春", "秋"]), ["春", "夏", "秋", "冬"])

    def test_evergreen_all_four(self):
        # 常青/四季 在上游 normalize_season 已展开为四季，兼容集应仍是全集（不丢季）
        self.assertEqual(
            set(season_compatible_set(["春", "夏", "秋", "冬"])),
            {"春", "夏", "秋", "冬"},
        )


class TestSeasonConflictCompat(unittest.TestCase):
    def test_spring_anchor_accepts_summer(self):
        # 关键回归：春锚点不应与夏款冲突
        self.assertFalse(season_conflict(["夏"], ["春"]))
        self.assertFalse(season_conflict(["春"], ["夏"]))

    def test_spring_anchor_rejects_autumn(self):
        # 春×秋 仍冲突（春夏 vs 秋冬 两季划分）
        self.assertTrue(season_conflict(["秋"], ["春"]))
        self.assertTrue(season_conflict(["春"], ["秋"]))

    def test_winter_outerwear_blocks_summer(self):
        # 保护初衷：冬装外套仍不应拉夏款下装
        self.assertTrue(season_conflict(["夏"], ["冬"]))

    def test_same_season_no_conflict(self):
        self.assertFalse(season_conflict(["春"], ["春"]))


class TestMilvusSeasonExprCompat(unittest.TestCase):
    def test_spring_intent_emits_summer_like(self):
        intent = UserIntent(season=["春"], gender="女")
        expr = _build_role_milvus_expr(intent, "bottoms")
        self.assertIn('season like "%春%"', expr)
        # 跨季兼容：春应同时放行夏
        self.assertIn('season like "%夏%"', expr)

    def test_autumn_intent_does_not_emit_summer(self):
        intent = UserIntent(season=["秋"], gender="女")
        expr = _build_role_milvus_expr(intent, "bottoms")
        self.assertIn('season like "%秋%"', expr)
        self.assertIn('season like "%冬%"', expr)
        # 秋冬组不应放行春夏组的夏
        self.assertNotIn('season like "%夏%"', expr)


class TestEsSeasonFilterCompat(unittest.TestCase):
    def test_spring_intent_emits_summer_wildcard(self):
        filters: list[dict] = []
        _append_season_sku(filters, ["春"], expand_compat=True)
        s = repr(filters)
        self.assertIn("*春*", s)
        # 跨季兼容：春应同时放行夏
        self.assertIn("*夏*", s)

    def test_autumn_intent_blocks_summer(self):
        filters: list[dict] = []
        _append_season_sku(filters, ["秋"], expand_compat=True)
        s = repr(filters)
        self.assertIn("*秋*", s)
        self.assertIn("*冬*", s)
        # 秋冬组不应放行春夏组的夏
        self.assertNotIn("*夏*", s)

    def test_default_no_compat_for_sku_search(self):
        # SKU 直接检索路径默认不展开（严格精确）
        filters: list[dict] = []
        _append_season_sku(filters, ["春"])
        s = repr(filters)
        self.assertIn("*春*", s)
        self.assertNotIn("*夏*", s)


if __name__ == "__main__":
    unittest.main()
