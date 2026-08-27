"""请求审计单测（config 开关 + ES 客户端降级 + sales_pitch 审计文档构造）。"""

from __future__ import annotations

import unittest

from backend.config import get_elasticsearch_indices, get_request_audit_enabled
from backend.es_client import EsClient
from backend.services.request_audit import (
    RequestAuditLogger,
    build_audit_search_body,
    build_sales_pitch_doc,
    now_iso,
    slim_audit_row,
)


class ElasticsearchIndicesRequestsTest(unittest.TestCase):
    def test_only_requests_key_returned(self) -> None:
        cfg = {"elasticsearch": {"indices": {
            "requests": "fila-requests", "skus": "s",
        }}}
        out = get_elasticsearch_indices(cfg)
        self.assertEqual(out, {"requests": "fila-requests"})

    def test_absent_returns_empty(self) -> None:
        out = get_elasticsearch_indices({"elasticsearch": {"indices": {}}})
        self.assertEqual(out, {})


class RequestAuditEnabledTest(unittest.TestCase):
    def test_default_true_when_absent(self) -> None:
        self.assertTrue(get_request_audit_enabled({"elasticsearch": {}}))

    def test_explicit_false(self) -> None:
        cfg = {"elasticsearch": {"request_audit": {"enabled": False}}}
        self.assertFalse(get_request_audit_enabled(cfg))

    def test_explicit_true(self) -> None:
        cfg = {"elasticsearch": {"request_audit": {"enabled": True}}}
        self.assertTrue(get_request_audit_enabled(cfg))


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

    def test_available_property(self) -> None:
        self.assertTrue(_make_es(_FakeEsClient(), {}).available)
        down = EsClient.__new__(EsClient)
        down._client = None  # type: ignore[attr-defined]
        self.assertFalse(down.available)


class NowIsoTest(unittest.TestCase):
    def test_iso_with_timezone(self) -> None:
        s = now_iso()
        self.assertIn("T", s)
        self.assertTrue("+" in s or s.endswith("Z"))


class BuildSalesPitchDocTest(unittest.TestCase):
    def _meta(self) -> dict:
        return {
            "trace_id": "tid", "session_id": "sid", "app_id": "app",
            "caller": "caller", "ts": "2026-08-26T10:00:00+08:00",
            "elapsed_ms": 12, "status": "ok", "error": None,
        }

    def test_ok_result_pitch_truncated(self) -> None:
        doc = build_sales_pitch_doc(
            input_block={"products": [{"title": "卫衣"}]},
            result={"pitch": "好" * 1000},
            meta=self._meta(),
        )
        self.assertEqual(doc["request_kind"], "sales_pitch")
        self.assertEqual(doc["status"], "ok")
        self.assertEqual(doc["result"]["pitch_len"], 1000)
        self.assertEqual(len(doc["result"]["pitch"]), 600)
        # 推荐管线的三个块不适用，固定置 None
        self.assertIsNone(doc["intent"])
        self.assertIsNone(doc["recall"])
        self.assertIsNone(doc["ranking"])

    def test_error_result(self) -> None:
        doc = build_sales_pitch_doc(
            input_block={},
            result={"error": "empty LLM output"},
            meta={**self._meta(), "status": "error", "error": "empty LLM output"},
        )
        self.assertEqual(doc["status"], "error")
        self.assertEqual(doc["result"]["error"], "empty LLM output")
        self.assertEqual(doc["result"]["pitch_len"], 0)
        self.assertIsNone(doc["result"]["pitch"])

    def test_none_result(self) -> None:
        doc = build_sales_pitch_doc(input_block={}, result=None, meta={})
        self.assertIsNone(doc["result"])


class AuditSearchBodyTest(unittest.TestCase):
    def test_empty_filters_match_all(self) -> None:
        body = build_audit_search_body({})
        self.assertEqual(body["query"], {"match_all": {}})
        self.assertEqual(body["sort"], [{"ts": {"order": "desc"}}])

    def test_term_and_range(self) -> None:
        body = build_audit_search_body({
            "trace_id": "tid", "app_id": "app", "request_kind": "sales_pitch",
            "ts_from": "2026-08-26", "ts_to": "2026-08-27", "size": "5",
            "offset": "10",
        })
        must = body["query"]["bool"]["must"]
        terms = {
            list(m["term"].keys())[0]: list(m["term"].values())[0]
            for m in must if "term" in m
        }
        self.assertEqual(terms["trace_id.keyword"], "tid")
        self.assertEqual(terms["request_kind.keyword"], "sales_pitch")
        rng = [m for m in must if "range" in m][0]["range"]["ts"]
        self.assertEqual(rng["gte"], "2026-08-26")
        self.assertEqual(rng["lte"], "2026-08-27")
        self.assertEqual(body["size"], 5)
        self.assertEqual(body["from"], 10)

    def test_size_clamped(self) -> None:
        body = build_audit_search_body({"size": 9999})
        self.assertEqual(body["size"], 200)
        # size=0 视为缺省(falsy)，落回默认 50
        body = build_audit_search_body({"size": 0})
        self.assertEqual(body["size"], 50)


class SlimAuditRowTest(unittest.TestCase):
    def test_slim(self) -> None:
        src = {
            "trace_id": "t", "session_id": "s", "app_id": "a",
            "request_kind": "sales_pitch", "ts": "x", "elapsed_ms": 9,
            "status": "ok",
            "input": {
                "customer": {"nickname": "王女士"},
                "products": [{"title": "t1"}, {"title": "t2"}],
            },
            "result": {"pitch": "话术", "pitch_len": 2},
        }
        row = slim_audit_row(src)
        self.assertEqual(row["trace_id"], "t")
        self.assertEqual(row["product_count"], 2)
        self.assertTrue(row["has_customer"])
        self.assertEqual(row["pitch_len"], 2)

    def test_slim_no_customer_no_result(self) -> None:
        row = slim_audit_row({"input": {"products": []}, "result": None})
        self.assertEqual(row["product_count"], 0)
        self.assertFalse(row["has_customer"])
        self.assertEqual(row["pitch_len"], 0)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
