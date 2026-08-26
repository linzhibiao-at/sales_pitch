from __future__ import annotations

import unittest

from backend.search_debug.milvus_service import (
    get_milvus_config,
    milvus_hybrid_debug,
)


def _stub_searcher():
    """不连云端的假 searcher，记录调用参数。"""

    class _Stub:
        def __init__(self):
            self.calls = []
            self.collection_name = "fila_sku_hybrid_vectors"
            self._ranker = "rrf"
            self._kw_w = 0.2
            self._sem_w = 0.8
            self._limit = 20
            self._nprobe = 16

        def _record(self, name, **kw):
            self.calls.append((name, kw))
            return [{"sku_id": f"{name}-1", "score": 0.9, "product_name": "x"}]

        def search_keyword(self, query, *, expr=None, limit=None,
                           output_fields=None, skip_rewrite=False):
            return self._record("keyword", query=query, expr=expr, limit=limit,
                                output_fields=output_fields, skip_rewrite=skip_rewrite)

        def search_semantic(self, query, *, expr=None, limit=None,
                            output_fields=None, skip_rewrite=False):
            return self._record("semantic", query=query, expr=expr, limit=limit,
                                output_fields=output_fields, skip_rewrite=skip_rewrite)

        def search_hybrid(self, query, *, expr=None, limit=None, kw_w=None,
                          sem_w=None, ranker=None, output_fields=None,
                          skip_rewrite=True):
            return self._record("hybrid", query=query, expr=expr, limit=limit,
                                kw_w=kw_w, sem_w=sem_w, ranker=ranker,
                                output_fields=output_fields, skip_rewrite=skip_rewrite)

    return _Stub()


class TestMilvusHybridDebug(unittest.IsolatedAsyncioTestCase):
    async def test_three_branches_with_stub(self):
        s = _stub_searcher()
        r = await milvus_hybrid_debug("白色T恤", top_k=5, expr='gender=="男"',
                                     searcher=s)
        self.assertEqual(r["query"], "白色T恤")
        self.assertEqual(r["params"]["top_k"], 5)
        self.assertEqual(r["params"]["ranker"], "rrf")
        for k in ("keyword", "semantic", "hybrid"):
            self.assertEqual(len(r[k]["hits"]), 1)
            self.assertIsNone(r[k]["error"])
            self.assertGreaterEqual(r[k]["took_ms"], 0)
        names = {c[0] for c in s.calls}
        self.assertEqual(names, {"keyword", "semantic", "hybrid"})
        # 三路都用同一 expr / limit / skip_rewrite
        for _, kw in s.calls:
            self.assertEqual(kw["expr"], 'gender=="男"')
            self.assertEqual(kw["limit"], 5)
            self.assertTrue(kw["skip_rewrite"])

    async def test_branch_error_captured(self):
        s = _stub_searcher()
        def _boom(*a, **k):
            raise RuntimeError("embed 失败")
        s.search_semantic = _boom
        r = await milvus_hybrid_debug("x", searcher=s)
        self.assertIsNone(r["keyword"]["error"])
        self.assertEqual(r["semantic"]["error"], "embed 失败")
        self.assertEqual(r["semantic"]["hits"], [])
        self.assertIsNone(r["hybrid"]["error"])

    async def test_rewrite_preview_when_skip_false(self):
        import backend.search_debug.milvus_service as mod
        from backend.retrieval.hybrid_search import RewriteResult

        original = mod.rewrite_query
        mod.rewrite_query = lambda q, f=None: RewriteResult(
            keyword_query="白色 T恤", semantic_query="白色T恤",
            filters={"gender": "男"}, source="rule")
        try:
            s = _stub_searcher()
            r = await milvus_hybrid_debug("白色T恤男", skip_rewrite=False, searcher=s)
        finally:
            mod.rewrite_query = original
        self.assertIsNotNone(r["rewrite"])
        self.assertEqual(r["rewrite"]["keyword_query"], "白色 T恤")
        self.assertEqual(r["rewrite"]["filters"], {"gender": "男"})
        # skip_rewrite=False 要透传给三路
        for _, kw in s.calls:
            self.assertFalse(kw["skip_rewrite"])

    async def test_rewrite_none_when_skip_true(self):
        s = _stub_searcher()
        r = await milvus_hybrid_debug("x", skip_rewrite=True, searcher=s)
        self.assertIsNone(r["rewrite"])

    async def test_hits_normalized_to_jsonable(self):
        """Milvus 数组字段返回 protobuf RepeatedContainer，必须归一为 list。"""

        class _Repeated:
            def __init__(self, items):
                self._items = items

            def __iter__(self):
                return iter(self._items)

        s = _stub_searcher()
        s.search_hybrid = lambda query, **kw: [
            {"sku_id": "S1", "score": 0.9, "features": _Repeated(["透气", "速干"]),
             "price": 299}]
        r = await milvus_hybrid_debug("x", searcher=s)
        hit = r["hybrid"]["hits"][0]
        self.assertEqual(hit["features"], ["透气", "速干"])
        self.assertIsInstance(hit["features"], list)
        self.assertEqual(hit["price"], 299)

    async def test_enrich_tryon_image_from_facade(self):
        """三路 hit 的 tryon_image 从 ES facade 补全（Milvus 集合无图片字段）。"""
        import backend.search_debug.milvus_service as mod

        class _FakeFacade:
            def __init__(self):
                self.called_with = None

            def get_skus(self, sku_ids):
                self.called_with = list(sku_ids)
                # 只给 keyword-1 / hybrid-1 图片，semantic-1 不返回（模拟 ES 未命中）
                return [{"sku_id": "keyword-1", "tryon_image": "http://img/kw1.jpg"},
                        {"sku_id": "hybrid-1", "tryon_image": "http://img/hyb.jpg"}]

        orig = mod._get_facade
        fake = _FakeFacade()
        mod._get_facade = lambda: fake
        try:
            s = _stub_searcher()
            r = await milvus_hybrid_debug("白色T恤", searcher=s)
        finally:
            mod._get_facade = orig
        self.assertEqual(r["keyword"]["hits"][0]["tryon_image"], "http://img/kw1.jpg")
        self.assertEqual(r["hybrid"]["hits"][0]["tryon_image"], "http://img/hyb.jpg")
        # semantic-1 在 ES 未命中 → 空串
        self.assertEqual(r["semantic"]["hits"][0]["tryon_image"], "")
        # 去重后仍把三路 sku_id 都查了
        self.assertIn("keyword-1", fake.called_with)
        self.assertIn("semantic-1", fake.called_with)

    async def test_enrich_tryon_image_resilient_to_facade_error(self):
        """facade 抛错时不应让三路结果失败，tryon_image 退为空串。"""
        import backend.search_debug.milvus_service as mod

        class _BadFacade:
            def get_skus(self, sku_ids):
                raise RuntimeError("ES 挂了")

        orig = mod._get_facade
        mod._get_facade = lambda: _BadFacade()
        try:
            s = _stub_searcher()
            r = await milvus_hybrid_debug("x", searcher=s)
        finally:
            mod._get_facade = orig
        self.assertIsNone(r["keyword"]["error"])
        self.assertEqual(r["keyword"]["hits"][0]["tryon_image"], "")


class TestGetMilvusConfig(unittest.TestCase):
    def test_shape(self):
        cfg = get_milvus_config()
        for k in ("collection", "ranker", "keyword_weight", "semantic_weight",
                  "default_limit", "nprobe", "output_fields", "hybrid_supported"):
            self.assertIn(k, cfg)
        self.assertIsInstance(cfg["output_fields"], list)
        self.assertIn("sku_id", cfg["output_fields"])
        self.assertIsInstance(cfg["hybrid_supported"], bool)


class TestMilvusDebugEndpoints(unittest.TestCase):
    """端点 wiring：用 TestClient + monkeypatch，不连云端。"""

    def _client(self):
        from fastapi.testclient import TestClient
        from backend import main as appmod
        return TestClient(appmod.app)

    def setUp(self):
        from backend import main as appmod
        self._appmod = appmod
        self._orig_cfg = appmod.get_milvus_config
        self._orig_run = appmod.milvus_hybrid_debug

    def tearDown(self):
        self._appmod.get_milvus_config = self._orig_cfg
        self._appmod.milvus_hybrid_debug = self._orig_run

    def test_config_endpoint(self):
        self._appmod.get_milvus_config = lambda: {"collection": "fila_sku_hybrid_vectors",
                                                   "hybrid_supported": True}
        r = self._client().get("/api/search-debug/milvus/config")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["collection"], "fila_sku_hybrid_vectors")

    def test_hybrid_search_happy_path(self):
        async def _fake(query, **kw):
            return {"query": query, "params": kw, "rewrite": None,
                    "keyword": {"hits": [], "took_ms": 1.0, "error": None},
                    "semantic": {"hits": [], "took_ms": 1.0, "error": None},
                    "hybrid": {"hits": [], "took_ms": 1.0, "error": None}}
        self._appmod.milvus_hybrid_debug = _fake
        r = self._client().post("/api/search-debug/milvus/hybrid-search",
                                json={"query": "x", "top_k": 5})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["query"], "x")

    def test_validation_empty_query(self):
        r = self._client().post("/api/search-debug/milvus/hybrid-search",
                                json={"query": "  "})
        # 400 或 422 都接受：query 校验在端点内 raise 400，pydantic 不拦空串
        self.assertIn(r.status_code, (400, 422))

    def test_validation_bad_ranker(self):
        r = self._client().post("/api/search-debug/milvus/hybrid-search",
                                json={"query": "x", "ranker": "nope"})
        self.assertIn(r.status_code, (400, 422))

    def test_validation_bad_top_k(self):
        r = self._client().post("/api/search-debug/milvus/hybrid-search",
                                json={"query": "x", "top_k": 0})
        self.assertIn(r.status_code, (400, 422))


if __name__ == "__main__":
    unittest.main()
