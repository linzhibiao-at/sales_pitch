"""请求审计单测（config 开关 + MySQL 客户端降级 + 后台批量写 + 审计文档构造）。"""

from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import MagicMock

from backend.config import get_mysql_table, get_mysql_url, get_request_audit_enabled
from backend.infra.mysql import MysqlClient, _parse_url, _to_json, _from_json, _row_to_doc
from backend.services.audit_worker import AuditBatchWorker
from backend.services.request_audit import (
    RequestAuditLogger,
    build_audit_query,
    build_sales_pitch_doc,
    now_iso,
    slim_audit_row,
)


# ── Config 测试 ────────────────────────────────────────────────────────

class MysqlUrlConfigTest(unittest.TestCase):
    def test_from_yaml(self) -> None:
        cfg = {"mysql": {"url": "mysql+pymysql://user:pass@localhost/db"}}
        self.assertEqual(get_mysql_url(cfg), "mysql+pymysql://user:pass@localhost/db")

    def test_empty_when_absent(self) -> None:
        self.assertEqual(get_mysql_url({}), "")

    def test_env_override(self) -> None:
        import os
        os.environ["MYSQL_URL"] = "mysql+pymysql://env:test@host/db"
        try:
            self.assertEqual(get_mysql_url({}), "mysql+pymysql://env:test@host/db")
        finally:
            del os.environ["MYSQL_URL"]


class MysqlTableConfigTest(unittest.TestCase):
    def test_default(self) -> None:
        self.assertEqual(get_mysql_table({}), "request_audit")

    def test_from_yaml(self) -> None:
        cfg = {"mysql": {"table": "my_audit"}}
        self.assertEqual(get_mysql_table(cfg), "my_audit")


class RequestAuditEnabledTest(unittest.TestCase):
    def test_default_true_when_absent(self) -> None:
        self.assertTrue(get_request_audit_enabled({}))

    def test_explicit_false(self) -> None:
        cfg = {"request_audit": {"enabled": False}}
        self.assertFalse(get_request_audit_enabled(cfg))

    def test_explicit_true(self) -> None:
        cfg = {"request_audit": {"enabled": True}}
        self.assertTrue(get_request_audit_enabled(cfg))


# ── MySQL URL 解析 ─────────────────────────────────────────────────────

class ParseMysqlUrlTest(unittest.TestCase):
    def test_basic(self) -> None:
        params = _parse_url("mysql+pymysql://user:pass@host:3306/mydb?charset=utf8mb4")
        self.assertEqual(params["host"], "host")
        self.assertEqual(params["port"], 3306)
        self.assertEqual(params["user"], "user")
        self.assertEqual(params["password"], "pass")
        self.assertEqual(params["database"], "mydb")
        self.assertEqual(params["charset"], "utf8mb4")

    def test_no_port(self) -> None:
        params = _parse_url("mysql+pymysql://u:p@h/db")
        self.assertEqual(params["port"], 3306)


# ── 辅助函数测试 ───────────────────────────────────────────────────────

class JsonHelpersTest(unittest.TestCase):
    def test_to_json_none(self) -> None:
        self.assertIsNone(_to_json(None))

    def test_to_json_dict(self) -> None:
        result = _to_json({"a": 1})
        self.assertEqual(json.loads(result), {"a": 1})

    def test_from_json_none(self) -> None:
        self.assertIsNone(_from_json(None))

    def test_from_json_string(self) -> None:
        self.assertEqual(_from_json('{"a": 1}'), {"a": 1})

    def test_from_json_invalid(self) -> None:
        self.assertEqual(_from_json("not json"), "not json")


class RowToDocTest(unittest.TestCase):
    def test_basic(self) -> None:
        row = {
            "id": 1,
            "trace_id": "t1",
            "session_id": "s1",
            "app_id": "app",
            "caller": "caller",
            "request_kind": "sales_pitch",
            "ts": "2026-09-01T10:00:00",
            "created_at": "2026-09-01 10:00:00",
            "elapsed_ms": 100,
            "status": "ok",
            "error": None,
            "input_json": '{"products": [{"title": "test"}]}',
            "result_json": '{"pitch": "话术", "pitch_len": 2}',
            "intent_json": None,
            "recall_json": None,
            "ranking_json": None,
        }
        doc = _row_to_doc(row)
        self.assertEqual(doc["id"], 1)
        self.assertEqual(doc["trace_id"], "t1")
        self.assertEqual(doc["input"]["products"][0]["title"], "test")
        self.assertEqual(doc["result"]["pitch"], "话术")
        self.assertIsNone(doc["intent"])


# ── 审计文档构造测试 ──────────────────────────────────────────────────

class NowIsoTest(unittest.TestCase):
    def test_iso_with_timezone(self) -> None:
        s = now_iso()
        self.assertIn("T", s)
        self.assertTrue("+" in s or s.endswith("Z"))


class BuildSalesPitchDocTest(unittest.TestCase):
    def _meta(self) -> dict:
        return {
            "trace_id": "tid", "session_id": "sid", "app_id": "app",
            "caller": "caller", "ts": "2026-09-01T10:00:00+08:00",
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


# ── SQL 查询构造测试 ──────────────────────────────────────────────────

class BuildAuditQueryTest(unittest.TestCase):
    def test_empty_filters(self) -> None:
        where, params, limit, offset = build_audit_query({})
        self.assertEqual(where, "")
        self.assertEqual(params, [])
        self.assertEqual(limit, 50)
        self.assertEqual(offset, 0)

    def test_term_filters(self) -> None:
        where, params, limit, offset = build_audit_query({
            "trace_id": "tid", "app_id": "app", "request_kind": "sales_pitch",
        })
        self.assertIn("trace_id = %s", where)
        self.assertIn("app_id = %s", where)
        self.assertIn("request_kind = %s", where)
        self.assertEqual(params, ["tid", "app", "sales_pitch"])

    def test_time_range(self) -> None:
        where, params, limit, offset = build_audit_query({
            "ts_from": "2026-09-01", "ts_to": "2026-09-02",
        })
        self.assertIn("ts >= %s", where)
        self.assertIn("ts <= %s", where)
        self.assertEqual(params, ["2026-09-01", "2026-09-02"])

    def test_pagination(self) -> None:
        where, params, limit, offset = build_audit_query({"size": 20, "offset": 40})
        self.assertEqual(limit, 20)
        self.assertEqual(offset, 40)

    def test_size_clamped(self) -> None:
        _, _, limit, _ = build_audit_query({"size": 9999})
        self.assertEqual(limit, 200)
        _, _, limit, _ = build_audit_query({"size": 0})
        self.assertEqual(limit, 50)


# ── 精简行测试 ────────────────────────────────────────────────────────

class SlimAuditRowTest(unittest.TestCase):
    def test_slim(self) -> None:
        src = {
            "trace_id": "t", "session_id": "s", "app_id": "a",
            "request_kind": "sales_pitch", "ts": "x", "created_at": "x",
            "elapsed_ms": 9, "status": "ok",
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
        self.assertEqual(row["created_at"], "x")

    def test_slim_no_customer_no_result(self) -> None:
        row = slim_audit_row({"input": {"products": []}, "result": None})
        self.assertEqual(row["product_count"], 0)
        self.assertFalse(row["has_customer"])
        self.assertEqual(row["pitch_len"], 0)


# ── RequestAuditLogger 测试 ───────────────────────────────────────────

class _MockMysqlClient:
    """模拟 MysqlClient（只实现用到的方法）。"""

    def __init__(self) -> None:
        self._available = True
        self.docs: list[dict] = []
        self.batch_calls: list[list[dict]] = []
        self.query_calls: list[tuple] = []

    @property
    def available(self) -> bool:
        return self._available

    def insert_audit_many(self, docs: list[dict]) -> int:
        self.batch_calls.append(list(docs))
        self.docs.extend(docs)
        return len(docs)

    def query_audit(
        self, where: str, params: tuple | list,
        limit: int = 50, offset: int = 0,
    ) -> list[dict]:
        self.query_calls.append((where, params, limit, offset))
        # 简单模拟：返回所有 docs（忽略 WHERE）
        return self.docs[offset:offset + limit]

    def count_audit(self, where: str, params: tuple | list) -> int:
        return len(self.docs)

    def get_by_trace_id(self, trace_id: str) -> dict | None:
        for doc in self.docs:
            if doc.get("trace_id") == trace_id:
                return doc
        return None


def _make_logger(client, enabled: bool = True):
    """绕过 MysqlClient.__init__ 的 MySQL 连接，直接注入可测实例（纯查询用途）。"""
    log = RequestAuditLogger.__new__(RequestAuditLogger)
    log._client = client
    log._enabled = enabled
    log._worker = None
    return log


class RequestAuditLoggerTest(unittest.TestCase):
    def test_disabled_skips_write(self) -> None:
        mock = _MockMysqlClient()
        log = _make_logger(mock, enabled=False)
        log.write({"a": 1})
        self.assertEqual(mock.batch_calls, [])

    def test_write_queued_and_batch_written(self) -> None:
        """write 仅入队；flush 后后台线程已批量写入。"""
        mock = _MockMysqlClient()
        worker = AuditBatchWorker(mock)
        log = RequestAuditLogger(client=mock, enabled=True, worker=worker)
        try:
            log.write({"trace_id": "t1", "app_id": "app"})
            log.write({"trace_id": "t2", "app_id": "app"})
            self.assertTrue(log.flush())
        finally:
            worker.close()
        self.assertEqual(len(mock.docs), 2)
        self.assertEqual(mock.docs[0]["trace_id"], "t1")
        self.assertGreaterEqual(len(mock.batch_calls), 1)

    def test_write_batch_failure_swallowed(self) -> None:
        class _Boom:
            available = True

            def insert_audit_many(self, docs):
                raise RuntimeError("boom")

        log = RequestAuditLogger(client=_Boom(), enabled=True)
        try:
            log.write({"a": 1})
            self.assertTrue(log.flush())  # 不抛
            self.assertEqual(log._worker.stats()["failed_batches"], 1)
        finally:
            log.close()

    def test_query_only_instance_no_worker(self) -> None:
        """start_worker=False：纯查询实例，write 静默、不起线程。"""
        mock = _MockMysqlClient()
        log = RequestAuditLogger(client=mock, enabled=True, start_worker=False)
        self.assertIsNone(log._worker)
        log.write({"a": 1})
        self.assertEqual(mock.batch_calls, [])
        self.assertTrue(log.flush())

    def test_search_and_count(self) -> None:
        mock = _MockMysqlClient()
        mock.docs = [
            {"trace_id": "t1", "app_id": "a"},
            {"trace_id": "t2", "app_id": "b"},
        ]
        log = _make_logger(mock, enabled=True)
        rows = log.search("", [], limit=10, offset=0)
        self.assertEqual(len(rows), 2)
        total = log.count("", [])
        self.assertEqual(total, 2)

    def test_disabled_search_returns_empty(self) -> None:
        log = _make_logger(_MockMysqlClient(), enabled=False)
        self.assertEqual(log.search("", []), [])
        self.assertEqual(log.count("", []), 0)

    def test_get_by_trace_id(self) -> None:
        mock = _MockMysqlClient()
        mock.docs = [{"trace_id": "tid", "app_id": "a"}]
        log = _make_logger(mock, enabled=True)
        doc = log.get_by_trace_id("tid")
        self.assertIsNotNone(doc)
        self.assertEqual(doc["app_id"], "a")
        self.assertIsNone(log.get_by_trace_id("nope"))


class MysqlClientAvailabilityTest(unittest.TestCase):
    def test_unavailable_when_no_url(self) -> None:
        """无 MYSQL_URL 配置时，MysqlClient.available 为 False。"""
        # 绕过实际 MySQL 连接，直接测试 available 属性
        client = MysqlClient.__new__(MysqlClient)
        client._conn = None
        client._params = None
        client._table = ""
        client._lock = __import__("threading").Lock()
        self.assertFalse(client.available)

    def test_available_when_connected(self) -> None:
        client = MysqlClient.__new__(MysqlClient)
        client._conn = MagicMock()
        client._params = None
        client._table = "test"
        client._lock = __import__("threading").Lock()
        self.assertTrue(client.available)


class MysqlReconnectDeadlockTest(unittest.TestCase):
    """持锁操作触达重连路径（_ensure_conn→_connect→_create_table）不得死锁。

    历史缺陷：_create_table 内部拿 self._lock，而 insert/query/count 已持
    锁调用 _ensure_conn——非重入 Lock 下同线程二次拿锁永久阻塞。
    """

    def test_insert_reconnect_path_no_deadlock(self) -> None:
        from unittest.mock import patch

        with patch(
            "backend.infra.mysql.get_mysql_url",
            return_value="mysql+pymysql://u:p@h/db",
        ), patch(
            "backend.infra.mysql.get_mysql_table",
            return_value="request_audit",
        ), patch("backend.infra.mysql.pymysql") as fake:
            cur = fake.connect.return_value.cursor.return_value.__enter__.return_value
            cur.rowcount = 1
            client = MysqlClient()  # 真实 __init__：锁类型来自生产代码
            client._conn = None  # 模拟断连 / 首连失败后的重试状态

            result: dict = {}

            def run() -> None:
                result["n"] = client.insert_audit_many([{"trace_id": "t1"}])

            t = threading.Thread(target=run, daemon=True)
            t.start()
            t.join(5)
            self.assertFalse(
                t.is_alive(),
                "insert_audit_many 死锁：持锁重连路径重入 _create_table 二次拿锁",
            )
            self.assertEqual(result.get("n"), 1)


# ── 后台批量写线程测试 ──────────────────────────────────────────────

class _BlockingClient:
    """首次 insert 阻塞直到 release，用于确定性控制后台线程节奏。"""

    available = True

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.batches: list[int] = []

    def insert_audit_many(self, docs: list[dict]) -> int:
        self.started.set()
        self.release.wait(timeout=5)
        self.batches.append(len(docs))
        return len(docs)


class AuditBatchWorkerTest(unittest.TestCase):
    def test_flush_empty_returns_true(self) -> None:
        w = AuditBatchWorker(_MockMysqlClient())
        try:
            self.assertTrue(w.flush(timeout=1.0))
            self.assertEqual(w.stats(), {"queued": 0, "written": 0, "dropped": 0, "failed_batches": 0})
        finally:
            w.close()

    def test_close_drains_pending(self) -> None:
        """close 停线程前 drain 剩余队列，未处理文档不丢。"""
        mock = _MockMysqlClient()
        w = AuditBatchWorker(mock, poll_interval=60)  # 长轮询：线程暂不取
        for i in range(3):
            w.submit({"trace_id": f"t{i}"})
        w.close()
        self.assertEqual(len(mock.docs), 3)

    def test_queue_full_drops_new_doc(self) -> None:
        """队列满时 submit 丢弃新文档并计数，不抛异常。"""
        bc = _BlockingClient()
        w = AuditBatchWorker(bc, max_queue=2, poll_interval=0.01)
        try:
            self.assertTrue(w.submit({"trace_id": "d1"}))
            self.assertTrue(bc.started.wait(timeout=2))  # 线程已取走 d1 并阻塞
            self.assertTrue(w.submit({"trace_id": "d2"}))
            self.assertTrue(w.submit({"trace_id": "d3"}))  # 队列 2/2 满
            self.assertFalse(w.submit({"trace_id": "d4"}))  # 丢弃
            bc.release.set()
            self.assertTrue(w.flush())
        finally:
            w.close()
        stats = w.stats()
        self.assertEqual(stats["dropped"], 1)
        self.assertEqual(stats["written"], 3)  # d1 d2 d3

    def test_batch_size_respected(self) -> None:
        """每批不超过 batch_size，总量不丢。"""
        mock = _MockMysqlClient()
        w = AuditBatchWorker(mock, batch_size=2, poll_interval=0.01)
        try:
            for i in range(5):
                w.submit({"trace_id": f"t{i}"})
            self.assertTrue(w.flush())
        finally:
            w.close()
        self.assertEqual(len(mock.docs), 5)
        self.assertTrue(all(len(b) <= 2 for b in mock.batch_calls))

    def test_write_batch_exception_counted_not_raised(self) -> None:
        class _Boom:
            available = True

            def insert_audit_many(self, docs):
                raise RuntimeError("boom")

        w = AuditBatchWorker(_Boom())
        try:
            w.submit({"a": 1})
            self.assertTrue(w.flush())
        finally:
            w.close()
        self.assertEqual(w.stats()["failed_batches"], 1)
        self.assertEqual(w.stats()["written"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
