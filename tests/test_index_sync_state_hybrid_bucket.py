from __future__ import annotations

import unittest
from pathlib import Path

from scripts import index_sync_state as iss


class TestHybridBucket(unittest.TestCase):
    def test_normalize_keeps_hybrid_bucket(self):
        raw = {
            "sku_vectors": {"A": "1"},
            "sku_text_vectors": {"B": "2"},
            "sku_hybrid_vectors": {"C": "3"},
        }
        out = iss._normalize_milvus_state(raw)
        self.assertEqual(out["sku_hybrid_vectors"], {"C": "3"})

    def test_normalize_creates_hybrid_bucket_when_missing(self):
        out = iss._normalize_milvus_state({})
        self.assertIn("sku_hybrid_vectors", out)
        self.assertEqual(out["sku_hybrid_vectors"], {})

    def test_load_state_preserves_hybrid_bucket(self):
        p = Path(__file__).resolve().parent / "_tmp_hybrid_state.json"
        try:
            state = iss.load_state(p)
            state["milvus"]["sku_hybrid_vectors"] = {"SKU1": "sig"}
            iss.save_state(state, p)
            reloaded = iss.load_state(p)
            self.assertEqual(reloaded["milvus"]["sku_hybrid_vectors"], {"SKU1": "sig"})
        finally:
            p.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
