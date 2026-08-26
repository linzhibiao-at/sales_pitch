from __future__ import annotations

import unittest

from backend.config import get_elasticsearch_indices


class GetElasticsearchIndicesTest(unittest.TestCase):
    def test_reviews_optional_absent(self) -> None:
        cfg = {"elasticsearch": {"indices": {
            "skus": "s", "outfits": "o",
        }}}
        out = get_elasticsearch_indices(cfg)
        self.assertEqual(out, {"skus": "s", "outfits": "o"})
        self.assertNotIn("reviews", out)

    def test_reviews_optional_present(self) -> None:
        cfg = {"elasticsearch": {"indices": {
            "skus": "s", "outfits": "o", "reviews": "r",
        }}}
        out = get_elasticsearch_indices(cfg)
        self.assertEqual(out["reviews"], "r")

    def test_required_keys_still_enforced(self) -> None:
        cfg = {"elasticsearch": {"indices": {"skus": "s"}}}
        with self.assertRaises(ValueError):
            get_elasticsearch_indices(cfg)


from backend.retrieval.es_client import EsClient


def _make_es(fake_client, indices):
    """绕过 EsClient.__init__ 的 config/ping,直接装一个可测实例。"""
    es = EsClient.__new__(EsClient)
    es._client = fake_client  # type: ignore[attr-defined]
    es._indices = indices  # type: ignore[attr-defined]
    return es


class _FakeEsClient:
    """模拟 elasticsearch-py 客户端(只实现本测试用到的方法)。"""

    def __init__(self) -> None:
        self.docs = {}  # _id -> _source
        self._seq = 0

    def index(self, index, body, id=None, **kw):
        if id is None:
            self._seq += 1
            id = f"auto{self._seq}"
        self.docs[id] = dict(body)
        return {"result": "created", "_id": id}

    def get(self, index, id, **kw):
        if id in self.docs:
            return {"found": True, "_source": dict(self.docs[id])}
        return {"found": False}

    def delete(self, index, id, **kw):
        if id in self.docs:
            del self.docs[id]
            return {"result": "deleted"}
        return {"result": "not_found"}

    def search(self, index, body, **kw):
        rows = list(self.docs.items())
        return {"hits": {"hits": [{"_id": i, "_source": dict(s)} for i, s in rows]}}


class EsClientGenericMethodsTest(unittest.TestCase):
    def test_index_doc_auto_id(self) -> None:
        fake = _FakeEsClient()
        es = _make_es(fake, {"reviews": "r"})
        new_id = es.index_doc("reviews", {"data_file": "f", "rating": 5})
        self.assertEqual(new_id, "auto1")
        self.assertEqual(fake.docs["auto1"]["data_file"], "f")

    def test_index_doc_unavailable(self) -> None:
        es = _make_es(None, {"reviews": "r"})
        self.assertIsNone(es.index_doc("reviews", {"a": 1}))

    def test_delete_doc_hit_and_miss(self) -> None:
        fake = _FakeEsClient()
        fake.docs["x"] = {"a": 1}
        es = _make_es(fake, {"reviews": "r"})
        self.assertTrue(es.delete_doc("reviews", "x"))
        self.assertFalse(es.delete_doc("reviews", "x"))

    def test_search_docs(self) -> None:
        fake = _FakeEsClient()
        fake.docs["a"] = {"data_file": "f1"}
        fake.docs["b"] = {"data_file": "f2"}
        es = _make_es(fake, {"reviews": "r"})
        out = es.search_docs("reviews", {"query": {"match_all": {}}})
        ids = [doc_id for doc_id, _src in out]
        self.assertEqual(sorted(ids), ["a", "b"])

    def test_search_docs_unavailable(self) -> None:
        es = _make_es(None, {"reviews": "r"})
        self.assertEqual(es.search_docs("reviews", {"query": {}}), [])


import tempfile
from pathlib import Path

from eval.review_store import (
    ReviewStore,
    SqliteReviewStore,
    get_review_store,
)


class SqliteReviewStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = SqliteReviewStore(db_path=Path(self.tmp.name))

    def tearDown(self) -> None:
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_add_get_delete_roundtrip(self) -> None:
        row = self.store.add(
            data_file="f.jsonl", input_sku_id="S1", outfit_id="O1",
            rating=4, comment="好", reviewer="u", reviewer_role="买手",
            reviewer_name="张",
        )
        self.assertIn("id", row)
        self.assertEqual(row["rating"], 4)
        rows = self.store.get("f.jsonl")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["input_sku_id"], "S1")
        self.assertTrue(self.store.delete(str(row["id"])))
        self.assertEqual(self.store.get("f.jsonl"), [])

    def test_multiple_reviews_per_outfit(self) -> None:
        self.store.add(data_file="f", input_sku_id="S", outfit_id="O", rating=5)
        self.store.add(data_file="f", input_sku_id="S", outfit_id="O", rating=3)
        rows = self.store.get("f")
        self.assertEqual(len(rows), 2)
        # 按 id DESC(时间倒序)→ 第二条在前
        self.assertEqual(rows[0]["rating"], 3)

    def test_get_filters_by_data_file(self) -> None:
        self.store.add(data_file="a", input_sku_id="S", outfit_id="O")
        self.store.add(data_file="b", input_sku_id="S", outfit_id="O")
        self.assertEqual(len(self.store.get("a")), 1)


class GetReviewStoreFactoryTest(unittest.TestCase):
    def test_sqlite_by_default(self) -> None:
        # 显式 sqlite
        store = get_review_store({"review": {"storage": "sqlite"}})
        self.assertIsInstance(store, SqliteReviewStore)

    def test_unknown_value_falls_back_to_sqlite(self) -> None:
        store = get_review_store({"review": {"storage": "nope"}})
        self.assertIsInstance(store, SqliteReviewStore)

    def test_protocol_shape(self) -> None:
        store = SqliteReviewStore()
        self.assertIsInstance(store, ReviewStore)


from eval.es_review_store import EsReviewStore


class _FakeReviewEs:
    """假 EsClient(只实现 EsReviewStore 用到的三个方法)。"""

    def __init__(self) -> None:
        self.docs = {}  # _id -> _source
        self._seq = 0

    @property
    def available(self) -> bool:
        return True

    def index_doc(self, index_key, doc, doc_id=None):
        self._seq += 1
        new_id = f"es{self._seq}"
        self.docs[new_id] = dict(doc)
        return new_id

    def delete_doc(self, index_key, doc_id):
        if doc_id in self.docs:
            del self.docs[doc_id]
            return True
        return False

    def search_docs(self, index_key, body):
        term = body.get("query", {}).get("term", {})
        field, val = next(iter(term.items()))
        rows = [(_id, dict(s)) for _id, s in self.docs.items() if s.get(field) == val]
        rows.sort(key=lambda r: r[1].get("created_at", ""), reverse=True)
        return rows


class EsReviewStoreTest(unittest.TestCase):
    def test_add_returns_id(self) -> None:
        fake = _FakeReviewEs()
        store = EsReviewStore(es=fake)
        row = store.add(
            data_file="f.jsonl", input_sku_id="S1", outfit_id="O1",
            rating=4, comment="好", reviewer="u", reviewer_role="买手",
            reviewer_name="张",
        )
        self.assertEqual(row["id"], "es1")
        self.assertEqual(row["data_file"], "f.jsonl")
        self.assertIn("created_at", row)
        self.assertIn("updated_at", row)

    def test_get_filters_and_sorts_desc(self) -> None:
        fake = _FakeReviewEs()
        store = EsReviewStore(es=fake)
        store.add(data_file="a", input_sku_id="S", outfit_id="O", rating=1)
        store.add(data_file="b", input_sku_id="S", outfit_id="O", rating=2)
        store.add(data_file="a", input_sku_id="S", outfit_id="O", rating=3)
        rows = store.get("a")
        self.assertEqual(len(rows), 2)
        # created_at 倒序 → rating=3(后插)在前
        self.assertEqual(rows[0]["rating"], 3)

    def test_delete_hit_and_miss(self) -> None:
        fake = _FakeReviewEs()
        store = EsReviewStore(es=fake)
        row = store.add(data_file="f", input_sku_id="S", outfit_id="O")
        self.assertTrue(store.delete(row["id"]))
        self.assertFalse(store.delete(row["id"]))


class GetReviewStoreEsFactoryTest(unittest.TestCase):
    def test_es_returns_esreviewstore_when_available(self) -> None:
        # EsReviewStore() 会真实 ping ES;若环境无 ES,工厂已 try/except 回退 sqlite。
        # 故此处只断言:storage=es 时,若可构造则类型正确,否则回退 sqlite(两种都可接受)。
        store = get_review_store({"review": {"storage": "es"}})
        self.assertIn(type(store).__name__, ("EsReviewStore", "SqliteReviewStore"))


from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import main as main_mod


class ReviewEndpointsTest(unittest.TestCase):
    def _client(self):
        return TestClient(main_mod.app)

    def test_get_reviews_calls_store_get(self) -> None:
        fake = SqliteReviewStore()  # 用 sqlite 真 store,确保 get 返回 []
        with patch.object(main_mod, "get_review_store", return_value=fake):
            with self._client() as c:
                resp = c.get("/eval/api/reviews?file=none.jsonl")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_post_then_get_roundtrip(self) -> None:
        fake = SqliteReviewStore()
        with patch.object(main_mod, "get_review_store", return_value=fake):
            with self._client() as c:
                r = c.post("/eval/api/reviews", json={
                    "data_file": "t.jsonl", "input_sku_id": "S", "outfit_id": "O",
                    "rating": 5, "comment": "c",
                })
                self.assertEqual(r.status_code, 200)
                rid = r.json()["id"]
                g = c.get("/eval/api/reviews?file=t.jsonl")
                self.assertEqual(g.status_code, 200)
                self.assertEqual(len(g.json()), 1)
                d = c.delete(f"/eval/api/reviews?id={rid}")
                self.assertEqual(d.status_code, 200)

    def test_503_when_store_raises(self) -> None:
        class _Broken:
            available = True

            def add(self, **kw):
                raise RuntimeError("评审存储不可用(ES)")

            def get(self, f):
                raise RuntimeError("评审存储不可用(ES)")

            def delete(self, i):
                raise RuntimeError("评审存储不可用(ES)")

        with patch.object(main_mod, "get_review_store", return_value=_Broken()):
            with self._client() as c:
                self.assertEqual(c.get("/eval/api/reviews?file=x").status_code, 503)
                self.assertEqual(c.post("/eval/api/reviews", json={
                    "data_file": "x", "input_sku_id": "S", "outfit_id": "O"
                }).status_code, 503)
                self.assertEqual(c.delete("/eval/api/reviews?id=1").status_code, 503)


from scripts.build_fila_reviews_es_index import REVIEW_INDEX_MAPPING, build_index


class BuildReviewsIndexTest(unittest.TestCase):
    def test_mapping_has_required_fields(self) -> None:
        props = REVIEW_INDEX_MAPPING["mappings"]["properties"]
        for f in ("data_file", "input_sku_id", "outfit_id", "reviewer",
                  "reviewer_role", "reviewer_name"):
            self.assertIn(f, props)
            self.assertEqual(props[f]["type"], "keyword")
        self.assertEqual(props["rating"]["type"], "integer")
        self.assertEqual(props["comment"]["type"], "text")
        self.assertEqual(props["created_at"]["type"], "date")
        self.assertEqual(props["updated_at"]["type"], "date")

    def test_build_index_idempotent_skip_when_exists(self) -> None:
        calls = []

        class _Indices:
            @staticmethod
            def exists(index):
                return True

            @staticmethod
            def create(index, body):
                calls.append(("create", index, body))

            @staticmethod
            def delete(index, **kw):
                calls.append(("delete", index))

        class _Client:
            indices = _Indices()

        build_index(_Client(), "r", reset=False)
        self.assertEqual(calls, [])  # 已存在,跳过

    def test_build_index_reset_creates(self) -> None:
        calls = []

        class _Indices:
            @staticmethod
            def exists(index):
                return True

            @staticmethod
            def create(index, body):
                calls.append(("create", index))

            @staticmethod
            def delete(index, **kw):
                calls.append(("delete", index))

        class _Client:
            indices = _Indices()

        build_index(_Client(), "r", reset=True)
        self.assertEqual([c[0] for c in calls], ["delete", "create"])


if __name__ == "__main__":
    unittest.main()
