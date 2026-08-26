from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from pymilvus import FunctionType

import scripts.build_hybrid_index as bhi


class TestSchema(unittest.TestCase):
    def test_schema_has_bm25_function_and_fields(self):
        schema = bhi.build_hybrid_schema(dim=1024)
        names = [f.name for f in schema.fields]
        self.assertIn("sku_id", names)
        self.assertIn("search_text", names)
        self.assertIn("sparse_vector", names)
        self.assertIn("dense_vector", names)
        st = next(f for f in schema.fields if f.name == "search_text")
        self.assertTrue(st.params.get("enable_analyzer"))
        self.assertIn("chinese", str(st.params.get("analyzer_params")))
        funcs = schema.functions
        self.assertEqual(len(funcs), 1)
        fn = funcs[0]
        self.assertEqual(fn.name, "search_text_bm25")
        self.assertEqual(fn.type, FunctionType.BM25)
        self.assertEqual(fn.input_field_names, ["search_text"])
        self.assertEqual(fn.output_field_names, ["sparse_vector"])


class TestIndexParams(unittest.TestCase):
    def test_index_params(self):
        params = bhi.get_hybrid_index_params()
        by_field = {p["field_name"]: p for p in params}
        self.assertEqual(by_field["sparse_vector"]["index_type"], "SPARSE_INVERTED_INDEX")
        self.assertEqual(by_field["sparse_vector"]["metric_type"], "BM25")
        self.assertEqual(by_field["dense_vector"]["index_type"], "IVF_FLAT")
        self.assertEqual(by_field["dense_vector"]["metric_type"], "COSINE")


class TestBuildInsertRow(unittest.TestCase):
    def test_includes_search_text_omits_sparse(self):
        row = {"sku_id": "S1", "title": "短袖T", "brand_line": "FILA"}
        vec = [0.1] * 8
        rec = bhi.build_insert_row(row, vec, dim=8)
        self.assertEqual(rec["sku_id"], "S1")
        self.assertIn("search_text", rec)
        self.assertEqual(rec["dense_vector"], vec)
        self.assertNotIn("sparse_vector", rec)  # 服务端 BM25 Function 自动产
        self.assertIsInstance(rec["search_text"], str)

    def test_is_intimate_stored_lowercase_bool(self):
        # expr 用 is_intimate == "false" 过滤贴身；存储必须是小写 "true"/"false"
        rec_false = bhi.build_insert_row({"sku_id": "S1", "is_intimate": False}, [0.1] * 8, 8)
        self.assertEqual(rec_false["is_intimate"], "false")
        rec_true = bhi.build_insert_row({"sku_id": "S2", "is_intimate": True}, [0.1] * 8, 8)
        self.assertEqual(rec_true["is_intimate"], "true")
        rec_empty = bhi.build_insert_row({"sku_id": "S3"}, [0.1] * 8, 8)
        self.assertEqual(rec_empty["is_intimate"], "false")


class TestCreateCollectionCallsClient(unittest.TestCase):
    def test_create_invokes_create_collection(self):
        client = MagicMock()
        client.has_collection.return_value = False
        bhi.create_hybrid_collection(
            client, "fila_sku_hybrid_vectors", dim=1024, uri="http://cloud:19530"
        )
        self.assertTrue(client.create_collection.called)
        kw = client.create_collection.call_args.kwargs
        self.assertEqual(kw["collection_name"], "fila_sku_hybrid_vectors")
        self.assertIsNotNone(kw["schema"])
        self.assertIsNotNone(kw["index_params"])


if __name__ == "__main__":
    unittest.main()
