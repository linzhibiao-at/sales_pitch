from __future__ import annotations

import unittest

from backend.config import get_elasticsearch_indices, get_request_audit_enabled


class ElasticsearchIndicesRequestsTest(unittest.TestCase):
    def test_requests_optional_absent(self) -> None:
        cfg = {"elasticsearch": {"indices": {"skus": "s", "outfits": "o"}}}
        out = get_elasticsearch_indices(cfg)
        self.assertNotIn("requests", out)

    def test_requests_optional_present(self) -> None:
        cfg = {"elasticsearch": {"indices": {
            "skus": "s", "outfits": "o", "requests": "fila-requests",
        }}}
        out = get_elasticsearch_indices(cfg)
        self.assertEqual(out["requests"], "fila-requests")


class RequestAuditEnabledTest(unittest.TestCase):
    def test_default_true_when_absent(self) -> None:
        self.assertTrue(get_request_audit_enabled({"elasticsearch": {}}))

    def test_explicit_false(self) -> None:
        cfg = {"elasticsearch": {"request_audit": {"enabled": False}}}
        self.assertFalse(get_request_audit_enabled(cfg))

    def test_explicit_true(self) -> None:
        cfg = {"elasticsearch": {"request_audit": {"enabled": True}}}
        self.assertTrue(get_request_audit_enabled(cfg))


from backend.retrieval.es_client import EsClient


def _make_es(fake_client, indices):
    """绕过 EsClient.__init__ 的 config/ping，直接装可测实例。"""
    es = EsClient.__new__(EsClient)
    es._client = fake_client  # type: ignore[attr-defined]
    es._indices = indices  # type: ignore[attr-defined]
    return es


class _FakeEsClient:
    """模拟 elasticsearch-py 客户端（只实现用到的方法）。"""

    def __init__(self) -> None:
        self.docs = {}  # _id -> _source
        self.calls = []  # (index, body, id, refresh)
        self._seq = 0

    def index(self, index, body, id=None, **kw):
        self.calls.append((index, body, id, kw.get("refresh")))
        if id is None:
            self._seq += 1
            id = f"auto{self._seq}"
        self.docs[id] = dict(body)
        return {"result": "created", "_id": id}

    def search(self, index, body, **kw):
        q = (body or {}).get("query") or {"match_all": {}}
        size = int((body or {}).get("size") or len(self.docs))
        rows = [(i, dict(s)) for i, s in self.docs.items() if self._match(s, q)]
        return {"hits": {"hits": [
            {"_id": i, "_source": s} for i, s in rows[:size]
        ]}}

    @staticmethod
    def _match(src, q):
        if "match_all" in q:
            return True
        if "term" in q:
            return _FakeEsClient._term_match(src, q["term"])
        if "bool" in q:
            must = (q.get("bool") or {}).get("must") or []
            return all(_FakeEsClient._clause_match(src, c) for c in must)
        return True

    @staticmethod
    def _clause_match(src, c):
        if "term" in c:
            return _FakeEsClient._term_match(src, c["term"])
        return True  # range 等不在本测试模拟范围

    @staticmethod
    def _term_match(src, term):
        for field, val in term.items():
            base = field.split(".")[0]
            if str(src.get(base)) != str(val):
                return False
        return True


class EsClientIndexDocRefreshTest(unittest.TestCase):
    def test_refresh_true_default(self) -> None:
        fake = _FakeEsClient()
        es = _make_es(fake, {"requests": "r"})
        es.index_doc("requests", {"a": 1})
        self.assertEqual(fake.calls[-1][3], True)

    def test_refresh_false_passthrough(self) -> None:
        fake = _FakeEsClient()
        es = _make_es(fake, {"requests": "r"})
        es.index_doc("requests", {"a": 1}, refresh=False)
        self.assertEqual(fake.calls[-1][3], False)

    def test_unknown_index_key_returns_none(self) -> None:
        es = _make_es(_FakeEsClient(), {"requests": "r"})
        self.assertIsNone(es.index_doc("nope", {"a": 1}))

    def test_search_unknown_index_key_returns_empty(self) -> None:
        es = _make_es(_FakeEsClient(), {"requests": "r"})
        self.assertEqual(es.search_docs("nope", {"query": {"match_all": {}}}), [])


import base64
import hashlib

from backend.services.request_audit import (
    RequestAuditLogger,
    build_audit_search_body,
    build_input_block,
    build_recommend_doc,
    build_regenerate_doc,
    slim_audit_row,
)


class BuildInputBlockTest(unittest.TestCase):
    def test_image_sha1_from_base64(self) -> None:
        raw = b"\x89PNGfake"
        b64 = base64.b64encode(raw).decode()
        blk = build_input_block(
            input_sku_id="S1", image_url="http://x/a.jpg",
            image_base64=b64, message="m", tryon=True, reason_style=None,
        )
        self.assertEqual(blk["image_sha1"], hashlib.sha1(raw).hexdigest())
        self.assertTrue(blk["tryon"])

    def test_no_image(self) -> None:
        blk = build_input_block(input_sku_id="S1")
        self.assertIsNone(blk["image_sha1"])
        self.assertIsNone(blk["image_url"])
        self.assertFalse(blk["tryon"])

    def test_outfit_id_field(self) -> None:
        blk = build_input_block(outfit_id="O1")
        self.assertEqual(blk["outfit_id"], "O1")


class BuildRecommendDocTest(unittest.TestCase):
    def _captured(self):
        return {
            "intent": {
                "type": "intent", "intent": {"query_type": "outfit"},
                "method": "trie", "confidence": 0.5,
                "llm_fallback": False, "image_override": False,
                "anchor_source": "sku", "image_role": None,
            },
            "anchor_skus": {"type": "anchor_skus", "skus": [
                {"sku_id": "A1", "similarity": 0.9},
            ]},
            "recall_done": {
                "type": "recall_done", "mode": "per_channel",
                "recalled_sku_count": 5, "composed_outfit_count": 2,
                "before_dedupe": 7, "after_dedupe": 4, "roles": {"上装": 2},
            },
            "recall_progress": [
                {"type": "recall_progress", "path": "image_vector",
                 "count": 3, "elapsed_ms": 12},
            ],
            "ranking_reason_done": {
                "type": "ranking_reason_done", "input_count": 4,
                "output_count": 2, "scoring_method": "llm",
                "ranking_elapsed_ms": 80,
            },
            "outfits": [
                {"outfit_id": "O1", "outfit_rank": 0, "reason": "r", "items": [
                    {"sku_id": "A1", "role": "上装", "title": "t",
                     "spu_id": "P1", "id_goods": "G1", "sku_image_url": "http://x", "price": 9.9},
                ]},
            ],
        }

    def test_doc_shape_and_item_slimming(self) -> None:
        doc = build_recommend_doc(
            input_block=build_input_block(input_sku_id="S1"),
            captured=self._captured(),
            meta={"trace_id": "tid", "session_id": "sid", "app_id": "app",
                  "caller": "caller", "ts": "2026-07-28T10:00:00+08:00",
                  "elapsed_ms": 123, "status": "ok", "error": None},
        )
        self.assertEqual(doc["request_kind"], "recommend")
        self.assertEqual(doc["status"], "ok")
        self.assertEqual(doc["intent"]["method"], "trie")
        self.assertEqual(doc["recall"]["anchor_sku_id"], "A1")
        self.assertEqual(doc["recall"]["paths"]["image_vector"]["count"], 3)
        self.assertEqual(doc["ranking"]["scoring_method"], "llm")
        item = doc["result"]["outfits"][0]["items"][0]
        self.assertEqual(item["sku_id"], "A1")
        self.assertNotIn("sku_image_url", item)
        self.assertNotIn("price", item)
        self.assertEqual(item["id_goods"], "G1")

    def test_missing_intent_and_recall_are_none(self) -> None:
        doc = build_recommend_doc(
            input_block={}, captured={}, meta={"trace_id": "t"})
        self.assertIsNone(doc["intent"])
        self.assertIsNone(doc["recall"])
        self.assertEqual(doc["result"]["outfits"], [])


class BuildRegenerateDocTest(unittest.TestCase):
    def test_shape(self) -> None:
        doc = build_regenerate_doc(
            input_block=build_input_block(outfit_id="O1"),
            result={"outfit_id": "O1", "reason": "because"},
            meta={"trace_id": "t", "status": "ok", "error": None,
                  "ts": "x", "elapsed_ms": 5, "app_id": None, "caller": None,
                  "session_id": None},
        )
        self.assertEqual(doc["request_kind"], "regenerate_reason")
        self.assertIsNone(doc["intent"])
        self.assertIsNone(doc["recall"])
        self.assertEqual(doc["result"]["outfit_id"], "O1")

    def test_error_result(self) -> None:
        doc = build_regenerate_doc(
            input_block={},
            result={"outfit_id": "O1", "reason": None, "error": "not found"},
            meta={"trace_id": "t", "status": "error", "error": "not found",
                  "ts": "x", "elapsed_ms": 1, "app_id": None, "caller": None,
                  "session_id": None},
        )
        self.assertEqual(doc["status"], "error")
        self.assertEqual(doc["result"]["error"], "not found")


class AuditSearchBodyTest(unittest.TestCase):
    def test_empty_filters_match_all(self) -> None:
        body = build_audit_search_body({})
        self.assertEqual(body["query"], {"match_all": {}})
        self.assertEqual(body["sort"], [{"ts": {"order": "desc"}}])

    def test_term_and_range(self) -> None:
        body = build_audit_search_body({
            "trace_id": "tid", "app_id": "app", "request_kind": "recommend",
            "ts_from": "2026-07-28", "ts_to": "2026-07-29", "size": "5",
            "offset": "10",
        })
        must = body["query"]["bool"]["must"]
        terms = {list(m["term"].keys())[0]: list(m["term"].values())[0] for m in must if "term" in m}
        self.assertEqual(terms["trace_id.keyword"], "tid")
        self.assertEqual(terms["request_kind.keyword"], "recommend")
        rng = [m for m in must if "range" in m][0]["range"]["ts"]
        self.assertEqual(rng["gte"], "2026-07-28")
        self.assertEqual(body["size"], 5)
        self.assertEqual(body["from"], 10)


class SlimAuditRowTest(unittest.TestCase):
    def test_slim(self) -> None:
        src = {
            "trace_id": "t", "app_id": "a", "request_kind": "recommend",
            "ts": "x", "elapsed_ms": 9, "status": "ok",
            "input": {"input_sku_id": "S1"},
            "result": {"outfits": [{"items": [{}]}, {"items": [{}]}]},
        }
        row = slim_audit_row(src)
        self.assertEqual(row["trace_id"], "t")
        self.assertEqual(row["outfit_count"], 2)
        self.assertEqual(row["input_sku_id"], "S1")


class RequestAuditLoggerTest(unittest.TestCase):
    def test_disabled_skips_write(self) -> None:
        fake = _FakeEsClient()
        es = _make_es(fake, {"requests": "r"})
        log = RequestAuditLogger(es=es, enabled=False)
        log.write({"a": 1})
        self.assertEqual(fake.calls, [])

    def test_write_refresh_false(self) -> None:
        fake = _FakeEsClient()
        es = _make_es(fake, {"requests": "r"})
        log = RequestAuditLogger(es=es, enabled=True)
        log.write({"a": 1})
        self.assertEqual(fake.calls[-1][3], False)

    def test_write_swallows_exception(self) -> None:
        class _Boom:
            available = True
            _indices = {"requests": "r"}
            def index_doc(self, *a, **k):
                raise RuntimeError("boom")
        log = RequestAuditLogger(es=_Boom(), enabled=True)  # type: ignore[arg-type]
        log.write({"a": 1})  # 不 raise

    def test_search_and_get_by_trace_id(self) -> None:
        fake = _FakeEsClient()
        fake.docs["d1"] = {"trace_id": "tid", "app_id": "a"}
        es = _make_es(fake, {"requests": "r"})
        log = RequestAuditLogger(es=es, enabled=True)
        rows = log.search({"query": {"match_all": {}}})
        self.assertEqual(rows[0][1]["trace_id"], "tid")
        self.assertEqual(log.get_by_trace_id("tid")["app_id"], "a")
        self.assertIsNone(log.get_by_trace_id("nope"))

    def test_disabled_search_returns_empty(self) -> None:
        es = _make_es(_FakeEsClient(), {"requests": "r"})
        log = RequestAuditLogger(es=es, enabled=False)
        self.assertEqual(log.search({"query": {"match_all": {}}}), [])
        self.assertIsNone(log.get_by_trace_id("tid"))


import asyncio
from types import SimpleNamespace

from backend.models import ExternalRecommendRequest
from backend.services.recommend_service import RecommendService


class _FakeAudit:
    def __init__(self) -> None:
        self.docs: list[dict] = []
        self.enabled = True

    def write(self, doc: dict) -> None:
        self.docs.append(doc)


class _SubRecommendService(RecommendService):
    """跳过真实 __init__，只装审计所需的桩。"""

    def __init__(self) -> None:  # noqa: D401
        self._audit = _FakeAudit()
        self._data = SimpleNamespace(get_skus=lambda ids: [])
        self._chat_events = []

    async def chat_stream(self, req):  # type: ignore[override]
        for ev in self._chat_events:
            if isinstance(ev, BaseException):
                raise ev
            yield ev


class ExternalRecommendAuditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = _SubRecommendService()
        self.svc._chat_events = [
            {"type": "session_id", "session_id": "sid"},
            {"type": "intent", "intent": {"query_type": "outfit"},
             "method": "trie", "confidence": 0.5, "llm_fallback": False,
             "image_override": False, "anchor_source": "sku", "image_role": None},
            {"type": "anchor_skus", "skus": [{"sku_id": "A1"}]},
            {"type": "recall_progress", "path": "image_vector",
             "count": 2, "elapsed_ms": 5},
            {"type": "recall_done", "mode": "per_channel",
             "recalled_sku_count": 2, "composed_outfit_count": 1,
             "before_dedupe": 3, "after_dedupe": 2, "roles": {"上装": 1}},
            {"type": "ranking_reason_done", "input_count": 2,
             "output_count": 1, "scoring_method": "llm",
             "ranking_elapsed_ms": 9},
            {"type": "outfit_results", "outfits": [
                {"outfit_id": "O1", "items": [
                    {"sku_id": "A1", "role": "上装", "title": "t",
                     "spu_id": "P1", "id_goods": "G1",
                     "sku_image_url": "http://x", "price": 9.9},
                ]},
            ]},
        ]

    def _req(self) -> ExternalRecommendRequest:
        return ExternalRecommendRequest(
            app_id="app", input_sku_id="S1", message="m", tryon=False,
        )

    def test_writes_audit_doc_on_success(self) -> None:
        out = asyncio.run(self.svc.external_recommend(
            self._req(), trace_id="tid", app_id="app", caller="caller"))
        self.assertEqual(out["input_sku_id"], "S1")
        self.assertEqual(len(self.svc._audit.docs), 1)
        doc = self.svc._audit.docs[0]
        self.assertEqual(doc["request_kind"], "recommend")
        self.assertEqual(doc["status"], "ok")
        self.assertEqual(doc["intent"]["method"], "trie")
        self.assertEqual(doc["recall"]["anchor_sku_id"], "A1")
        self.assertEqual(doc["result"]["outfits"][0]["items"][0]["sku_id"], "A1")
        self.assertNotIn("sku_image_url", doc["result"]["outfits"][0]["items"][0])

    def test_writes_error_doc_and_reraises(self) -> None:
        self.svc._chat_events = [
            {"type": "session_id", "session_id": "sid"},
            {"type": "intent", "intent": {}, "method": "trie",
             "confidence": 0.5, "llm_fallback": False,
             "image_override": False, "anchor_source": None, "image_role": None},
            RuntimeError("boom"),
        ]
        with self.assertRaises(RuntimeError):
            asyncio.run(self.svc.external_recommend(
                self._req(), trace_id="tid", app_id="app", caller="c"))
        self.assertEqual(len(self.svc._audit.docs), 1)
        self.assertEqual(self.svc._audit.docs[0]["status"], "error")

    def test_disabled_skips_write(self) -> None:
        self.svc._audit.enabled = False
        asyncio.run(self.svc.external_recommend(
            self._req(), trace_id="tid", app_id="app", caller="c"))
        self.assertEqual(self.svc._audit.docs, [])


from backend.models import ExternalRegenerateReasonRequest


class ExternalRegenerateAuditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = _SubRecommendService()

    def test_ok_writes_doc(self) -> None:
        req = ExternalRegenerateReasonRequest(outfit_id="O1")
        self.svc._write_regenerate_audit(
            req, result={"outfit_id": "O1", "reason": "r"},
            status="ok", error=None, trace_id="tid",
            app_id="app", caller="caller", t0=0.0,
        )
        doc = self.svc._audit.docs[0]
        self.assertEqual(doc["request_kind"], "regenerate_reason")
        self.assertEqual(doc["status"], "ok")
        self.assertEqual(doc["result"]["reason"], "r")
        self.assertEqual(doc["input"]["outfit_id"], "O1")

    def test_error_result_captured(self) -> None:
        req = ExternalRegenerateReasonRequest(outfit_id="O1")
        self.svc._write_regenerate_audit(
            req, result={"outfit_id": "O1", "reason": None, "error": "not found"},
            status="error", error="not found", trace_id="tid",
            app_id=None, caller=None, t0=0.0,
        )
        doc = self.svc._audit.docs[0]
        self.assertEqual(doc["status"], "error")
        self.assertEqual(doc["result"]["error"], "not found")

    def test_disabled_skips(self) -> None:
        self.svc._audit.enabled = False
        req = ExternalRegenerateReasonRequest(outfit_id="O1")
        self.svc._write_regenerate_audit(
            req, result={"outfit_id": "O1", "reason": "r"},
            status="ok", error=None, trace_id="tid",
            app_id="app", caller="caller", t0=0.0,
        )
        self.assertEqual(self.svc._audit.docs, [])
