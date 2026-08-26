"""diverse_greedy 组合在稀缺 role 上的复用测试。

场景：anchor(top) + 2 条粉色裤(同桶) + 51 双白鞋(多桶)。
修复前：bottoms 桶 2 个 SKU 耗尽即停 → 只产 2 套，51 双鞋浪费。
修复后：稀缺 role 循环复用，富余 role 继续轮选 → 产 max_outfits 套，
同一件裤可配不同鞋（合理的不重复 look）。
"""

from __future__ import annotations

import unittest

from backend.services import synthetic_outfit as so


def _row(sid: str, role: str, cat: str, color: str = "白色系") -> dict:
    return {
        "sku_id": sid, "role": role, "category_l2": cat,
        "color_series": [color], "price": 100, "gender": ["女"],
        "season": ["夏"], "display_image": "x",
    }


class _Cfg:
    def __init__(self, strategy: str = "diverse_greedy"):
        self._c = {"recommend": {
            "compose_strategy": strategy,
            "default_sku_per_role": 100,
        }}

    def __call__(self, *a, **k):
        return self._c


class DiverseGreedyScarceRoleTest(unittest.TestCase):
    def setUp(self):
        self._orig = so.load_config
        so.load_config = _Cfg()

    def tearDown(self):
        so.load_config = self._orig

    def _anchor(self) -> dict:
        a = _row("ANC", "top", "短袖T恤", "白色系")
        a["_is_image_input_anchor"] = True
        return a

    def test_scarce_bottoms_reused_with_abundant_shoes(self):
        """2 裤 × 51 鞋 → 应产 max_outfits=10 套，而非被 bottoms 卡死在 2。"""
        anchor = self._anchor()
        bottoms = [_row(f"BT{i}", "bottoms", "梭织长裤", "粉色系") for i in range(2)]
        shoes = [
            _row(f"SH{b}_{i}", "shoes", f"鞋类{b}", "白色系")
            for b in range(5) for i in range(11)
        ]  # 51 双，5 个 (cat,color) 桶
        by_role = {"bottoms": bottoms, "shoes": shoes}

        outfits = so.compose_outfits_from_role_recall(
            anchor, by_role, max_outfits=10, picks_per_role=100,
            source="global_compose",
        )
        self.assertEqual(len(outfits), 10, "应产出 10 套（被 max_outfits 截断）")

        # 每套都含 anchor + 一条裤 + 一双鞋
        for o in outfits:
            skus = {it["sku_id"] for it in o["items"]}
            self.assertIn("ANC", skus)
            self.assertEqual(sum(1 for it in o["items"] if it["role"] == "bottoms"), 1)
            self.assertEqual(sum(1 for it in o["items"] if it["role"] == "shoes"), 1)

        # 鞋应分散：10 套里的鞋应 >= 5 种不同（轮选不同桶），不能全是同一双
        shoe_skus = {
            next(it["sku_id"] for it in o["items"] if it["role"] == "shoes")
            for o in outfits
        }
        self.assertGreaterEqual(len(shoe_skus), 5)

    def test_small_grid_terminates_without_duplicates(self):
        """2 裤 × 2 鞋（均单桶）：diverse_greedy 轮选产对角线 2 套，不无限循环、不重复。

        diverse_greedy 是轮选非笛卡尔，两端单桶时只产 min(角色供给) 套对角搭配——
        这是既有设计，本测试只验证修复后不回归、不死循环、不产重复。
        """
        anchor = self._anchor()
        bottoms = [_row(f"BT{i}", "bottoms", "梭织长裤", "粉色系") for i in range(2)]
        shoes = [_row(f"SH{i}", "shoes", "运动鞋", "白色系") for i in range(2)]
        by_role = {"bottoms": bottoms, "shoes": shoes}

        outfits = so.compose_outfits_from_role_recall(
            anchor, by_role, max_outfits=10, picks_per_role=100,
            source="global_compose",
        )
        sigs = set()
        for o in outfits:
            sig = "|".join(sorted(it["sku_id"] for it in o["items"]))
            sigs.add(sig)
        self.assertEqual(len(outfits), 2)
        self.assertEqual(len(sigs), 2, "不应有重复搭配")

    def test_single_bottom_many_shoes(self):
        """1 裤 × 5 鞋 → 5 套（同一条裤配不同鞋，都算不同 look）。"""
        anchor = self._anchor()
        bottoms = [_row("BT0", "bottoms", "梭织长裤", "粉色系")]
        shoes = [_row(f"SH{i}", "shoes", f"鞋类{i}", "白色系") for i in range(5)]
        by_role = {"bottoms": bottoms, "shoes": shoes}

        outfits = so.compose_outfits_from_role_recall(
            anchor, by_role, max_outfits=10, picks_per_role=100,
            source="global_compose",
        )
        self.assertEqual(len(outfits), 5)
        # 同一条裤出现在每套里
        for o in outfits:
            self.assertIn("BT0", {it["sku_id"] for it in o["items"]})


if __name__ == "__main__":
    unittest.main()
