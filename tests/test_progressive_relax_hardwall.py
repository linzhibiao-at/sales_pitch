"""硬墙 + 尾位守卫 + master-switch 回归测试。

- gender/season/age 永不出现在 relax_priority 中（硬墙）。
- up_time/price 在链尾（最后两个）。
- up_time/price 不应在更早的 soft 已达标时被丢弃（尾位守卫）。
- enable_progressive_relax=False 时各通路单次查询、无放宽。
"""
from __future__ import annotations

import unittest
from unittest import mock

from backend.retrieval.progressive_relax import (
    get_relax_config,
    run_with_progressive_relax,
)


class HardWallTest(unittest.TestCase):
    def test_gender_season_age_never_dropped(self):
        """driver 只丢 priority 中列出的 slot；gender/season/age 不在列表中。"""
        _, priority, _ = get_relax_config()
        for hard in ("gender", "season", "age"):
            self.assertNotIn(hard, priority)

    def test_up_time_price_at_tail(self):
        """up_time/price 必须在链尾（最后两个）。"""
        _, priority, _ = get_relax_config()
        self.assertEqual(priority[-2:], ["up_time", "price"])

    def test_tail_only_guard(self):
        """up_time/price 不应在更早的 soft 已达标时被丢弃。"""
        calls: list[set[str]] = []

        def search_fn(dropped: set[str]) -> list[str]:
            calls.append(set(dropped))
            # 'modeling' dropped already yields enough → must stop there
            if "modeling" in dropped:
                return ["h1", "h2"]
            return []

        hits, dropped = run_with_progressive_relax(
            search_fn,
            priority=["modeling", "length_class", "up_time", "price"],
            min_hits=2,
        )
        self.assertEqual(dropped, ["modeling"])
        self.assertNotIn("up_time", dropped)
        self.assertNotIn("price", dropped)

    def test_exhausts_to_hard_wall_without_touching_identity(self):
        """耗尽 soft 链仍 0 命中时，driver 不会构造 gender/season/age 丢弃。"""
        seen: list[set[str]] = []

        def search_fn(dropped: set[str]) -> list[str]:
            seen.append(set(dropped))
            return []

        hits, dropped = run_with_progressive_relax(
            search_fn,
            priority=["modeling", "color_series", "up_time", "price"],
            min_hits=1,
        )
        self.assertEqual(hits, [])
        self.assertEqual(dropped, ["modeling", "color_series", "up_time", "price"])
        # 没有任何 dropped 集合包含身份 slot
        for d in seen:
            self.assertNotIn("gender", d)
            self.assertNotIn("season", d)
            self.assertNotIn("age", d)


class EsRegressionTest(unittest.TestCase):
    def test_es_master_switch_off_no_fallback_dropped(self):
        """enable_progressive_relax=False 时 ES 路单次查询、meta 无 fallback_dropped。"""
        from backend.models import UserIntent
        from backend.services import outfit_recall as orc
        fake_es = mock.MagicMock()
        fake_es.available = True
        fake_es.search_skus_with_query.return_value = []  # always 0
        sku_r = mock.MagicMock()
        sku_r._es = fake_es
        sku_r.get_sku.return_value = None
        intent = UserIntent(
            anchor_role="top", target_roles=["bottoms"], gender="男", season=["秋"],
        )
        cfg = {"recommend": {
            "query2es_llm_enabled": False,
            "enable_category_l2_pairing": False,
            "enable_color_series_pairing": False,
            "es_top_n_per_role": 10,
        }}
        with mock.patch("backend.services.outfit_recall.load_config", return_value=cfg), \
             mock.patch("backend.services.outfit_recall.get_elasticsearch_indices",
                        return_value={"skus": "fila_skus"}), \
             mock.patch("backend.services.outfit_recall.es_top_n_per_role", return_value=10), \
             mock.patch("backend.services.outfit_recall.resolve_pairing_allowed_companions",
                        return_value=None), \
             mock.patch("backend.services.outfit_recall.get_relax_config",
                        return_value=(False, ["modeling"], 1)), \
             mock.patch("backend.services.outfit_recall._debug_recall_io_enabled",
                        return_value=False):
            by_role, meta = orc.recall_query2es_skus(sku_r, intent, {"role": "top", "sku_id": "A1"})
        self.assertNotIn("bottoms", by_role)  # 0 hits, no relax
        self.assertNotIn("fallback_dropped", meta.get("bottoms", {}))
        # 单次查询（master switch off）
        self.assertEqual(fake_es.search_skus_with_query.call_count, 1)


if __name__ == "__main__":
    unittest.main()
