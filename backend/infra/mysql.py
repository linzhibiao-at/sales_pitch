"""MySQL 可选客户端（请求审计落库）。

MySQL 不可用时所有方法静默降级，不影响话术主链路。
使用 pymysql 驱动，连接串格式：

    mysql+pymysql://user:pass@host:port/database?charset=utf8mb4
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any
from urllib.parse import urlparse

from backend.config import get_mysql_table, get_mysql_url

try:
    import pymysql
except ImportError:  # pragma: no cover - requirements 已固定，防御性兜底
    pymysql = None

logger = logging.getLogger(__name__)

# 建表 DDL
_CREATE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS {table} (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    trace_id      VARCHAR(128),
    session_id    VARCHAR(128),
    app_id        VARCHAR(64),
    caller        VARCHAR(128),
    request_kind  VARCHAR(64),
    ts            VARCHAR(64),
    elapsed_ms    INT,
    status        VARCHAR(16),
    error         TEXT,
    input_json    LONGTEXT,
    intent_json   TEXT,
    recall_json   TEXT,
    ranking_json  TEXT,
    result_json   TEXT,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    INDEX idx_trace_id   (trace_id),
    INDEX idx_session_id (session_id),
    INDEX idx_app_id     (app_id),
    INDEX idx_ts         (ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

# 审计写入 SQL（executemany 批量时 pymysql 自动重写为多值插入）
_INSERT_AUDIT_SQL = """\
INSERT INTO {table}
    (trace_id, session_id, app_id, caller, request_kind,
     ts, elapsed_ms, status, error,
     input_json, intent_json, recall_json, ranking_json, result_json)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
"""


def _parse_url(url: str) -> dict[str, Any]:
    """从 ``mysql+pymysql://...`` 连接串中解析连接参数。"""
    parsed = urlparse(url)
    db = parsed.path.lstrip("/") or ""
    # 处理 query string 中的 charset 等参数
    charset = "utf8mb4"
    if parsed.query:
        for part in parsed.query.split("&"):
            if part.startswith("charset="):
                charset = part.split("=", 1)[1]
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 3306,
        "user": parsed.username or "",
        "password": parsed.password or "",
        "database": db,
        "charset": charset,
    }


class MysqlClient:
    """pymysql 客户端：自动建表 + 单连接（锁保护）+ 断连自动重连。"""

    def __init__(self) -> None:
        self._conn: Any = None
        self._params: dict[str, Any] | None = None
        self._table: str = ""
        # RLock（可重入）：持锁操作 → _ensure_conn → _connect → _create_table
        # 同线程重入拿锁，非重入 Lock 会在此路径死锁
        self._lock = threading.RLock()
        url = get_mysql_url()
        if not url:
            return
        if pymysql is None:
            logger.warning("[infra] pymysql 未安装，MySQL 审计不可用")
            return
        self._params = _parse_url(url)
        self._table = get_mysql_table()
        self._connect()

    def _connect(self) -> None:
        """按已存参数建立连接并自动建表；失败静默降级。"""
        if not self._params or pymysql is None:
            return
        try:
            self._conn = pymysql.connect(
                **self._params,
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=True,
                connect_timeout=5,
                read_timeout=10,
                write_timeout=10,
            )
            self._create_table()
        except Exception as e:  # noqa: BLE001
            logger.warning("[infra] MySQL init failed: %s", e)
            self._conn = None

    @property
    def available(self) -> bool:
        return self._conn is not None

    def _ensure_conn(self) -> bool:
        """确保连接可用：ping 保活 / 断连重连（含首次失败后的延迟重试）。"""
        if self._conn is not None:
            try:
                self._conn.ping(reconnect=True)
                return True
            except Exception:  # noqa: BLE001
                self._conn = None
        if self._params is not None:
            self._connect()
        return self._conn is not None

    def _create_table(self) -> None:
        if not self._conn:
            return
        with self._lock, self._conn.cursor() as cur:
            cur.execute(_CREATE_TABLE_SQL.format(table=self._table))
        logger.info("[infra] MySQL 审计表 '%s' 就绪", self._table)

    # ── 写 ─────────────────────────────────────────────────────────────
    def insert_audit_many(self, docs: list[dict[str, Any]]) -> int:
        """批量写入审计文档（executemany），返回成功写入条数。

        断连时重连后重试一次；仍失败则本批丢弃（返回 0），由调用方
        （后台批量写线程）决定后续策略。
        """
        if not docs:
            return 0
        rows = [_doc_row(d) for d in docs]
        sql = _INSERT_AUDIT_SQL.format(table=self._table)
        with self._lock:
            if not self._ensure_conn():
                return 0
            for attempt in (1, 2):
                try:
                    with self._conn.cursor() as cur:
                        cur.executemany(sql, rows)
                        return int(cur.rowcount or 0)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "[infra] MySQL insert_audit_many failed (attempt %d): %s",
                        attempt, e,
                    )
                    if attempt == 2 or not self._ensure_conn():
                        return 0
        return 0

    # ── 查 ─────────────────────────────────────────────────────────────
    def query_audit(
        self,
        where: str,
        params: tuple | list,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """按条件查询审计记录（分页，ts 倒序）。"""
        with self._lock:
            if not self._ensure_conn():
                return []
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        f"""SELECT id, trace_id, session_id, app_id, caller,
                                   request_kind, ts, created_at, elapsed_ms, status, error,
                                   input_json, result_json,
                                   intent_json, recall_json, ranking_json
                            FROM {self._table}
                            {where}
                            ORDER BY ts DESC
                            LIMIT %s OFFSET %s""",
                        (*params, limit, offset),
                    )
                    return [_row_to_doc(r) for r in cur.fetchall()]
            except Exception as e:  # noqa: BLE001
                logger.warning("[infra] MySQL query_audit failed: %s", e)
                return []

    def count_audit(self, where: str, params: tuple | list) -> int:
        """按条件统计总数。"""
        with self._lock:
            if not self._ensure_conn():
                return 0
            try:
                with self._conn.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) AS cnt FROM {self._table} {where}", params)
                    return int(cur.fetchone().get("cnt") or 0)
            except Exception as e:  # noqa: BLE001
                logger.warning("[infra] MySQL count_audit failed: %s", e)
                return 0

    def get_by_trace_id(self, trace_id: str) -> dict[str, Any] | None:
        """按 trace_id 查单条。"""
        rows = self.query_audit(
            "WHERE trace_id = %s", (trace_id,), limit=1,
        )
        return rows[0] if rows else None


# ── 辅助函数 ────────────────────────────────────────────────────────────

def _doc_row(doc: dict[str, Any]) -> tuple:
    """审计文档 → INSERT 参数行。"""
    return (
        doc.get("trace_id"), doc.get("session_id"), doc.get("app_id"),
        doc.get("caller"), doc.get("request_kind"),
        doc.get("ts"), doc.get("elapsed_ms"), doc.get("status"), doc.get("error"),
        _to_json(doc.get("input")), _to_json(doc.get("intent")),
        _to_json(doc.get("recall")), _to_json(doc.get("ranking")),
        _to_json(doc.get("result")),
    )


def _to_json(v: Any) -> str | None:
    """将 Python 对象序列化为 JSON 字符串存入 TEXT 列。"""
    if v is None:
        return None
    try:
        return json.dumps(v, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(v)


def _from_json(v: str | None) -> Any:
    """从 TEXT 列反序列化 JSON。"""
    if v is None:
        return None
    try:
        return json.loads(v)
    except (json.JSONDecodeError, TypeError):
        return v


def _row_to_doc(row: dict[str, Any]) -> dict[str, Any]:
    """将 MySQL 行转为审计文档 dict。"""
    return {
        "id": row.get("id"),
        "trace_id": row.get("trace_id"),
        "session_id": row.get("session_id"),
        "app_id": row.get("app_id"),
        "caller": row.get("caller"),
        "request_kind": row.get("request_kind"),
        "ts": row.get("ts"),
        "created_at": row.get("created_at"),
        "elapsed_ms": row.get("elapsed_ms"),
        "status": row.get("status"),
        "error": row.get("error"),
        "input": _from_json(row.get("input_json")),
        "intent": _from_json(row.get("intent_json")),
        "recall": _from_json(row.get("recall_json")),
        "ranking": _from_json(row.get("ranking_json")),
        "result": _from_json(row.get("result_json")),
    }
