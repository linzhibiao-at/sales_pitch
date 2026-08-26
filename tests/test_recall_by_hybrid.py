from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from backend.retrieval.sku_retriever import SkuRetriever


def _make_searcher(hits_per_kw: dict[str, list[tuple[str, float]]]):
    searcher = MagicMock()

    def _hq(query, *, expr=None, limit=None, output_fields=None, skip_rewrite=False):
        return [{"sku_id": sid, "score": sc} for sid, sc in hits_per_kw.get(query, [])]

    searcher.search_hybrid.side_effect = _hq
    return searcher


class TestRecallByHybrid(unittest.TestCase):
    def _retriever(self, searcher):
        r = SkuRetriever.__new__(SkuRetriever)
        r._milvus = MagicMock()
        r._milvus.hit_to_similarity.side_effect = lambda x: float(x)
        r._es = MagicMock()
        r._store = MagicMock()
        r._data = MagicMock()
        r._hybrid = searcher
        return r

    def test_merge_by_max_per_keyword(self):
        searcher = _make_searcher({
            "短袖": [("S1", 0.9), ("S2", 0.5)],
            "T恤": [("S1", 0.7), ("S3", 0.6)],
        })
        r = self._retriever(searcher)
        rows = r.recall_by_hybrid(
            ["短袖", "T恤"], category_l2_filter=None,
            color_series_filter=None, group_brand=None,
            attr_expr=None, trace_id="t1", fallback_on_empty=False,
        )
        merged = {sid: sim for sid, sim, _ in rows}
        self.assertAlmostEqual(merged["S1"], 0.9)  # 取 max
        self.assertAlmostEqual(merged["S2"], 0.5)
        self.assertAlmostEqual(merged["S3"], 0.6)

    def test_no_internal_fallback_on_empty(self):
        # 新契约：recall_by_hybrid 0 命中不再内部 fallback 到 dense；
        # hybrid→dense 的双 leg 由调用方 recall_text_vector_skus 的 search_fn 承担。
        searcher = _make_searcher({"短袖": []})
        r = self._retriever(searcher)
        r.recall_by_text_vector_keywords = MagicMock(return_value=[("S9", 0.3, 0.3)])
        rows = r.recall_by_hybrid(
            ["短袖"], category_l2_filter=None,
            color_series_filter=None, group_brand=None,
            attr_expr=None, trace_id="t2", fallback_on_empty=True,
        )
        self.assertFalse(r.recall_by_text_vector_keywords.called)
        self.assertEqual(rows, [])

    def test_empty_keywords(self):
        r = self._retriever(_make_searcher({}))
        self.assertEqual(r.recall_by_hybrid([]), [])


if __name__ == "__main__":
    unittest.main()
