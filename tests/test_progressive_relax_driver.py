"""progressive_relax 驱动器单元测试：0 命中时按优先级逐个丢弃 slot 直到命中数达标。"""
from __future__ import annotations

import unittest

from backend.retrieval.progressive_relax import run_with_progressive_relax


class ProgressiveRelaxDriverTest(unittest.TestCase):
    def test_returns_immediately_when_nonempty(self):
        calls: list[set[str]] = []

        def search_fn(dropped: set[str]) -> list[str]:
            calls.append(set(dropped))
            return ["x"]  # non-empty on first call

        hits, dropped = run_with_progressive_relax(
            search_fn, priority=["modeling", "color_series"], min_hits=1,
        )
        self.assertEqual(hits, ["x"])
        self.assertEqual(dropped, [])
        self.assertEqual(calls, [set()])  # only one call, no drops

    def test_drops_in_priority_order_until_nonempty(self):
        # search_fn returns [] until 'modeling' and 'length_class' are both dropped
        calls: list[set[str]] = []

        def search_fn(dropped: set[str]) -> list[str]:
            calls.append(set(dropped))
            if {"modeling", "length_class"}.issubset(dropped):
                return ["hit"]
            return []

        hits, dropped = run_with_progressive_relax(
            search_fn,
            priority=["modeling", "length_class", "color_series"],
            min_hits=1,
        )
        self.assertEqual(hits, ["hit"])
        # modeling dropped (still []), then length_class dropped (now nonempty) → stop
        self.assertEqual(dropped, ["modeling", "length_class"])
        self.assertEqual(
            calls,
            [set(), {"modeling"}, {"modeling", "length_class"}],
        )

    def test_exhausts_priority_and_stops_at_hard_wall(self):
        # never reaches a non-empty set → exhausts list, returns last (empty) result
        calls: list[set[str]] = []

        def search_fn(dropped: set[str]) -> list[str]:
            calls.append(set(dropped))
            return []  # always empty

        hits, dropped = run_with_progressive_relax(
            search_fn, priority=["modeling", "color_series"], min_hits=1,
        )
        self.assertEqual(hits, [])
        self.assertEqual(dropped, ["modeling", "color_series"])
        self.assertEqual(
            calls,
            [set(), {"modeling"}, {"modeling", "color_series"}],
        )

    def test_empty_priority_runs_once(self):
        calls: list[set[str]] = []

        def search_fn(dropped: set[str]) -> list[str]:
            calls.append(set(dropped))
            return []

        hits, dropped = run_with_progressive_relax(search_fn, priority=[], min_hits=1)
        self.assertEqual(hits, [])
        self.assertEqual(dropped, [])
        self.assertEqual(calls, [set()])

    def test_reordering_priority_changes_drop_order(self):
        calls: list[set[str]] = []

        def search_fn(dropped: set[str]) -> list[str]:
            calls.append(set(dropped))
            return ["h"] if "color_series" in dropped else []

        hits, dropped = run_with_progressive_relax(
            search_fn, priority=["color_series", "modeling"], min_hits=1,
        )
        self.assertEqual(dropped, ["color_series"])
        self.assertEqual(calls, [set(), {"color_series"}])

    def test_min_hits_threshold(self):
        # min_hits=3: 1 hit is not enough, keep dropping
        def search_fn(dropped: set[str]) -> list[str]:
            if "modeling" in dropped and "color_series" not in dropped:
                return ["a"]  # below threshold
            if {"modeling", "color_series"}.issubset(dropped):
                return ["a", "b", "c"]
            return []

        hits, dropped = run_with_progressive_relax(
            search_fn, priority=["modeling", "color_series"], min_hits=3,
        )
        self.assertEqual(len(hits), 3)
        self.assertEqual(dropped, ["modeling", "color_series"])


if __name__ == "__main__":
    unittest.main()
