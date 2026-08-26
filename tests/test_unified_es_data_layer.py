from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.local_data_store import LocalDataStore
from scripts.build_fila_es_index import create_index, outfit_doc, sku_doc


class UnifiedEsDataLayerTest(unittest.TestCase):
    def test_create_index_uses_es7_body_parameter(self) -> None:
        calls: list[dict[str, object]] = []

        class Indices:
            @staticmethod
            def create(**kwargs):
                calls.append(kwargs)

        class Client:
            indices = Indices()

        body = {"mappings": {"properties": {"sku_id": {"type": "keyword"}}}}
        create_index(Client(), "idx", body)

        self.assertEqual(calls, [{"index": "idx", "body": body}])

    def test_sku_doc_keeps_display_fields_for_runtime_cards(self) -> None:
        doc = sku_doc(
            {
                "sku_id": "SKU1",
                "spu_id": "SPU1",
                "title": "FILA 测试上衣",
                "search_text": "fila top",
                "search_keywords": "A12M631101FMA,FILA,男,运动",
                "category_l1": "服装",
                "category_l2": "T恤",
                "category_l3": "短袖T恤",
                "up_down_raw": "上装",
                "display_image": "https://example.com/display.jpg",
                "index_images": ["https://example.com/index.jpg"],
                "tryon_image": "https://example.com/tryon.jpg",
                "all_images": [
                    {
                        "path": "https://example.com/big.jpg",
                        "id_pa": "123",
                        "order_id": 1,
                        "image_type": "big",
                    },
                ],
                "ai_select": {
                    "path": "https://example.com/ai.jpg",
                    "note": "白底正面",
                    "candidate_count": "5",
                    "chosen_id_pa": "123",
                    "chosen_order_id": "2",
                    "chosen_image_type": "master",
                },
                "image_quality": {"is_tryon_ready": True},
                "material": "棉",
                "sub_series": "基础系列",
                "color_family": "白色",
                "modeling": "宽松",
            },
        )

        self.assertEqual(doc["display_image"], "https://example.com/display.jpg")
        self.assertEqual(doc["index_images"], ["https://example.com/index.jpg"])
        self.assertEqual(doc["tryon_image"], "https://example.com/tryon.jpg")
        self.assertEqual(doc["search_keywords"], "A12M631101FMA,FILA,男,运动")
        self.assertEqual(
            doc["all_images"],
            [
                {
                    "path": "https://example.com/big.jpg",
                    "id_pa": "123",
                    "order_id": 1,
                    "image_type": "big",
                },
            ],
        )
        self.assertEqual(doc["ai_select"]["path"], "https://example.com/ai.jpg")
        self.assertEqual(doc["category_l1"], "服装")
        self.assertEqual(doc["category_l3"], "短袖T恤")
        self.assertEqual(doc["up_down_raw"], "上装")
        self.assertEqual(doc["image_quality"], {"is_tryon_ready": True})
        self.assertEqual(doc["material"], "棉")
        self.assertEqual(doc["sub_series"], "基础系列")
        self.assertEqual(doc["color_family"], "白色")
        self.assertEqual(doc["modeling"], "宽松")

    def test_outfit_doc_indexes_items_and_sku_ids_from_preview(self) -> None:
        doc = outfit_doc(
            {
                "outfit_id": "OUT1",
                "name": "测试搭配",
                "source": "cc_material",
                "display_image": "https://example.com/outfit.jpg",
                "master_sku_id": "SKU1",
                "master_spu_id": "SPU1",
                "items": [
                    {
                        "sku_id": "SKU1",
                        "spu_id": "SPU1",
                        "role": "top",
                        "title": "上衣",
                        "price": 399,
                        "display_image": "top.jpg",
                        "tryon_image": "top-tryon.jpg",
                        "is_master": True,
                    },
                    {
                        "sku_id": "SKU2",
                        "spu_id": "SPU2",
                        "role": "bottoms",
                        "title": "裤子",
                        "price": 499,
                        "display_image": "pants.jpg",
                    },
                ],
            },
        )

        self.assertEqual(doc["sku_ids"], ["SKU1", "SKU2"])
        self.assertEqual(doc["spu_ids"], ["SPU1", "SPU2"])
        self.assertEqual(doc["display_image"], "https://example.com/outfit.jpg")
        self.assertEqual(doc["master_sku_id"], "SKU1")
        self.assertEqual(doc["items"][0]["sku_id"], "SKU1")
        self.assertEqual(doc["items"][0]["is_master"], True)
        self.assertEqual(doc["items"][1]["role"], "bottoms")

    def test_local_store_uses_preview_outfits_without_edges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            processed = root / "data" / "processed"
            preview = root / "data" / "preview"
            processed.mkdir(parents=True)
            preview.mkdir(parents=True)
            (processed / "skus.jsonl").write_text(
                json.dumps({"sku_id": "SKU1", "title": "上衣"})
                + "\n"
                + json.dumps({"sku_id": "SKU2", "title": "裤子"})
                + "\n",
                encoding="utf-8",
            )
            (processed / "spu_to_skus.json").write_text(
                json.dumps({"SPU1": ["SKU1"]}),
                encoding="utf-8",
            )
            (preview / "fila_outfits.json").write_text(
                json.dumps(
                    [
                        {
                            "outfit_id": "OUT1",
                            "items": [
                                {"sku_id": "SKU1", "role": "top"},
                                {"sku_id": "SKU2", "role": "bottoms"},
                            ],
                        },
                    ],
                ),
                encoding="utf-8",
            )
            cfg = {
                "paths": {
                    "processed_dir": "data/processed",
                    "preview_outfits_json": "data/preview/fila_outfits.json",
                },
            }
            with (
                patch("backend.local_data_store.get_root", return_value=root),
                patch("backend.local_data_store.load_config", return_value=cfg),
            ):
                store = LocalDataStore()
                store.load()

        self.assertEqual(store.spu_to_skus["SPU1"], ["SKU1"])
        self.assertIn("OUT1", store.outfits)
        self.assertEqual(store.sku_to_outfits["SKU1"], ["OUT1"])
        self.assertFalse(hasattr(store, "relations"))
        self.assertFalse(hasattr(store, "relations_by_source"))

    def test_data_facade_extracts_companion_skus_from_outfit_items(self) -> None:
        from backend.retrieval.data_facade import DataFacade

        facade = DataFacade(es=None)
        facade.outfits_by_sku = lambda sku_id, size=100: [
            {
                "outfit_id": "OUT1",
                "items": [
                    {"sku_id": "SKU1", "role": "top"},
                    {"sku_id": "SKU2", "role": "bottoms"},
                    {"sku_id": "SKU3", "role": "shoes"},
                ],
            },
        ]
        facade.get_sku = lambda sku_id: {
            "SKU2": {"sku_id": "SKU2", "role": "bottoms"},
            "SKU3": {"sku_id": "SKU3", "role": "shoes"},
        }.get(sku_id)

        rows, outfit_ids = facade.companion_skus_by_anchor(
            "SKU1",
            target_roles=["bottoms"],
        )

        self.assertEqual(outfit_ids, ["OUT1"])
        self.assertEqual(rows, [{"sku_id": "SKU2", "role": "bottoms"}])

    def test_data_facade_limits_outfit_recall_to_operational_sources(self) -> None:
        from backend.retrieval.data_facade import (
            OPERATIONAL_OUTFIT_SOURCES,
            DataFacade,
        )

        calls: list[dict[str, object]] = []

        class Es:
            available = True

            @staticmethod
            def search_outfits_by_sku(sku_id, size=100, *, sources=None):
                calls.append({"sku_id": sku_id, "size": size, "sources": sources})
                return []

        facade = DataFacade(es=Es())
        rows = facade.outfits_by_sku("SKU1", size=12)

        self.assertEqual(rows, [])
        self.assertEqual(
            calls,
            [{
                "sku_id": "SKU1",
                "size": 12,
                "sources": list(OPERATIONAL_OUTFIT_SOURCES),
            }],
        )

    def test_es_outfit_recall_filters_out_batch_eval_sources(self) -> None:
        from backend.retrieval.es_client import EsClient

        calls: list[dict[str, object]] = []

        class Client:
            @staticmethod
            def search(index, body):
                calls.append({"index": index, "body": body})
                return {"hits": {"hits": []}}

        es = EsClient.__new__(EsClient)
        es._client = Client()
        es._indices = {"outfits": "outfits-index"}

        rows = es.search_outfits_by_sku(
            "SKU1",
            size=5,
            sources=["cc_material", "micro_guide"],
        )

        self.assertEqual(rows, [])
        self.assertEqual(
            calls[0]["body"],
            {
                "size": 5,
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"sku_ids": "SKU1"}},
                            {"terms": {"source": ["cc_material", "micro_guide"]}},
                        ],
                    },
                },
            },
        )

    def test_batch_eval_keeps_synthetic_snapshot_and_tryon_meta(self) -> None:
        from eval.batch_eval import slim_outfits

        outfits = [
            {
                "outfit_id": "synth_1",
                "is_synthetic": True,
                "recall_source": "query2es_compose",
                "rank_score": 0.8,
                "outfit_tryon_image": "tryon.jpg",
                "items": [{"sku_id": "SKU1"}],
            },
        ]

        outfit_ids, meta = slim_outfits(outfits)

        self.assertEqual(outfit_ids, ["synth_1"])
        self.assertEqual(meta[0]["outfit_tryon_image"], "tryon.jpg")
        self.assertEqual(meta[0]["snapshot"], outfits[0])

    def test_batch_eval_outfit_es_builds_eval_doc_and_delete_query(self) -> None:
        from eval.batch_eval_outfit_es import (
            batch_eval_delete_query,
            batch_eval_outfit_id,
            batch_eval_source,
            build_batch_eval_docs,
        )

        docs = build_batch_eval_docs(
            [
                {
                    "outfit_id": "OUT1",
                    "recall_source": "query2es_compose",
                    "name": "测试搭配",
                    "items": [
                        {"sku_id": "SKU1", "spu_id": "SPU1", "role": "top"},
                        {"sku_id": "SKU2", "spu_id": "SPU2", "role": "bottoms"},
                    ],
                },
            ],
            input_sku_id="SKU1",
            input_sku={"title": "输入商品", "gender": "男"},
        )

        self.assertEqual(batch_eval_source("query2es_compose"), "batch_eval_query2es_compose")
        self.assertEqual(len(docs), 1)
        doc_id, doc = docs[0]
        self.assertEqual(doc_id, batch_eval_outfit_id("SKU1", "OUT1", 1))
        self.assertEqual(doc["source"], "batch_eval_query2es_compose")
        self.assertEqual(doc["original_outfit_id"], "OUT1")
        self.assertEqual(doc["sku_ids"], ["SKU1", "SKU2"])
        self.assertEqual(doc["batch_eval_input_sku_id"], "SKU1")

        self.assertEqual(
            batch_eval_delete_query(input_sku_ids=["SKU1"]),
            {
                "bool": {
                    "filter": [
                        {"prefix": {"source": "batch_eval_"}},
                        {"terms": {"batch_eval_input_sku_id": ["SKU1"]}},
                    ],
                },
            },
        )
        self.assertEqual(
            batch_eval_delete_query(sources=["batch_eval_query2es_compose"]),
            {
                "bool": {
                    "filter": [
                        {"terms": {"source": ["batch_eval_query2es_compose"]}},
                    ],
                },
            },
        )

    def test_es_count_and_delete_docs_by_query_use_outfits_index(self) -> None:
        from backend.retrieval.es_client import EsClient

        calls: list[dict[str, object]] = []

        class Client:
            @staticmethod
            def count(index, body):
                calls.append({"method": "count", "index": index, "body": body})
                return {"count": 3}

            @staticmethod
            def delete_by_query(index, body, refresh, conflicts):
                calls.append({
                    "method": "delete_by_query",
                    "index": index,
                    "body": body,
                    "refresh": refresh,
                    "conflicts": conflicts,
                })
                return {"deleted": 3}

        es = EsClient.__new__(EsClient)
        es._client = Client()
        es._indices = {"outfits": "outfits-index"}
        query = {"prefix": {"source": "batch_eval_"}}

        self.assertEqual(es.count_docs("outfits", query), 3)
        self.assertEqual(es.delete_docs_by_query("outfits", query), 3)
        self.assertEqual(
            calls,
            [
                {
                    "method": "count",
                    "index": "outfits-index",
                    "body": {"query": query},
                },
                {
                    "method": "delete_by_query",
                    "index": "outfits-index",
                    "body": {"query": query},
                    "refresh": True,
                    "conflicts": "proceed",
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
