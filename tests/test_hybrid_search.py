from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from backend.retrieval import hybrid_search as hs


class TestRuleRewrite(unittest.TestCase):
    def test_extracts_gender_and_price(self):
        r = hs.rule_rewrite("男款 100元以内", {})
        self.assertEqual(r.filters.get("gender"), "男")
        self.assertEqual(r.filters.get("price_max"), 100)

    def test_extracts_price_range(self):
        r = hs.rule_rewrite("100-300元", {})
        self.assertEqual(r.filters.get("price_min"), 100)
        self.assertEqual(r.filters.get("price_max"), 300)

    def test_keeps_plain_keyword(self):
        r = hs.rule_rewrite("短袖T", {})
        self.assertEqual(r.keyword_query, "短袖T")

    def test_season_attr(self):
        r = hs.rule_rewrite("夏季短袖", {})
        self.assertEqual(r.filters.get("season"), "夏季")


class TestBuildFilterExpr(unittest.TestCase):
    def test_price_and_gender(self):
        expr = hs.build_filter_expr({"price_min": 100, "price_max": 300, "gender": "男"})
        self.assertIn("price >= 100", expr)
        self.assertIn("price <= 300", expr)
        self.assertIn('gender == "男"', expr)

    def test_empty(self):
        self.assertEqual(hs.build_filter_expr(None), "")
        self.assertEqual(hs.build_filter_expr({}), "")


class TestFormatResults(unittest.TestCase):
    def test_parses_dict_hits(self):
        raw = [[
            {"id": "S1", "distance": 0.9, "entity": {"sku_id": "S1", "title": "短袖T"}},
            {"id": "S2", "distance": 0.5, "entity": {"sku_id": "S2", "title": "短裤"}},
        ]]
        items = hs.format_results(raw, output_fields=["sku_id", "title"])
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["sku_id"], "S1")
        self.assertAlmostEqual(items[0]["score"], 0.9)

    def test_empty(self):
        self.assertEqual(hs.format_results([], output_fields=["sku_id"]), [])


class TestSearchHybridCallsClient(unittest.TestCase):
    def test_invokes_hybrid_search(self):
        client = MagicMock()
        client.hybrid_search.return_value = [[
            {"id": "S1", "distance": 0.8, "entity": {"sku_id": "S1", "title": "T"}}
        ]]
        searcher = hs.FilaSkuHybridSearcher(client=client)
        items = searcher.search_hybrid(
            "短袖T", expr='role == "top"', limit=5, output_fields=["sku_id", "title"]
        )
        self.assertTrue(client.hybrid_search.called)
        call = client.hybrid_search.call_args
        self.assertEqual(call.kwargs["collection_name"], searcher.collection_name)
        self.assertEqual(len(call.kwargs["reqs"]), 2)
        self.assertEqual(call.kwargs["reqs"][0].anns_field, "sparse_vector")
        self.assertEqual(call.kwargs["reqs"][1].anns_field, "dense_vector")
        self.assertEqual(items[0]["sku_id"], "S1")


if __name__ == "__main__":
    unittest.main()
