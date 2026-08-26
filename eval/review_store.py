"""评审意见存储。支持 SQLite 与 ES 两种后端(由 config.review.storage 硬切换)。

SQLite 实现支持每套搭配多人多次评审;ES 实现见 eval.es_review_store。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent / "reviews.db"

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS outfit_reviews (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    data_file      TEXT NOT NULL,
    input_sku_id   TEXT NOT NULL,
    outfit_id      TEXT NOT NULL,
    rating         INTEGER,
    comment        TEXT,
    reviewer       TEXT,
    reviewer_role  TEXT,
    reviewer_name  TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
)
"""

_LEGACY_UNIQUE_MARKER = "UNIQUE(data_file, input_sku_id, outfit_id)"


@runtime_checkable
class ReviewStore(Protocol):
    def add(
        self, *,
        data_file: str,
        input_sku_id: str,
        outfit_id: str,
        rating: Optional[int] = None,
        comment: Optional[str] = None,
        reviewer: Optional[str] = None,
        reviewer_role: Optional[str] = None,
        reviewer_name: Optional[str] = None,
    ) -> dict: ...

    def get(self, data_file: str) -> list[dict]: ...

    def delete(self, id: str) -> bool: ...


class SqliteReviewStore:
    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = Path(db_path) if db_path else _DB_PATH

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        self._migrate(conn)
        return conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        cur = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='outfit_reviews'"
        )
        row = cur.fetchone()
        if row is None:
            conn.execute(_CREATE_SQL)
            conn.commit()
            return
        sql = row["sql"] or ""
        if _LEGACY_UNIQUE_MARKER not in sql:
            self._ensure_columns(conn)
            conn.commit()
            return
        conn.executescript(
            """
            BEGIN;
            ALTER TABLE outfit_reviews RENAME TO outfit_reviews_legacy;
            """ + _CREATE_SQL + """
            INSERT INTO outfit_reviews
                (data_file, input_sku_id, outfit_id, rating, comment, reviewer,
                 reviewer_role, reviewer_name, created_at, updated_at)
            SELECT
                data_file, input_sku_id, outfit_id, rating, comment, reviewer,
                NULL, NULL, created_at, updated_at
            FROM outfit_reviews_legacy;
            DROP TABLE outfit_reviews_legacy;
            COMMIT;
            """
        )

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(outfit_reviews)").fetchall()}
        if "reviewer_role" not in cols:
            conn.execute("ALTER TABLE outfit_reviews ADD COLUMN reviewer_role TEXT")
        if "reviewer_name" not in cols:
            conn.execute("ALTER TABLE outfit_reviews ADD COLUMN reviewer_name TEXT")

    def add(
        self, *,
        data_file: str,
        input_sku_id: str,
        outfit_id: str,
        rating: Optional[int] = None,
        comment: Optional[str] = None,
        reviewer: Optional[str] = None,
        reviewer_role: Optional[str] = None,
        reviewer_name: Optional[str] = None,
    ) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            cur = conn.execute(
                """
                INSERT INTO outfit_reviews
                    (data_file, input_sku_id, outfit_id, rating, comment,
                     reviewer, reviewer_role, reviewer_name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (data_file, input_sku_id, outfit_id, rating, comment,
                 reviewer, reviewer_role, reviewer_name, now, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM outfit_reviews WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return dict(row)
        finally:
            conn.close()

    def get(self, data_file: str) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM outfit_reviews WHERE data_file=? ORDER BY id DESC",
                (data_file,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def delete(self, id: str) -> bool:
        conn = self._connect()
        try:
            cur = conn.execute(
                "DELETE FROM outfit_reviews WHERE id = ?", (str(id),)
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


_default_cache: dict[str, ReviewStore] = {}


def get_review_store(cfg: Optional[dict] = None) -> ReviewStore:
    """按 config.review.storage 返回 ReviewStore(sqlite|es 硬切换)。

    仅当 ``cfg`` 为 None(走默认 config.yaml)时缓存单例;显式传 cfg
    (如测试)每次返回新实例,以便按不同配置选取后端。
    """
    if cfg is None:
        cached = _default_cache.get("singleton")
        if cached is not None:
            return cached
        from backend.config import load_config
        cfg = load_config()
    storage = str((cfg.get("review") or {}).get("storage") or "sqlite").strip().lower()
    store: ReviewStore
    if storage == "es":
        try:
            from eval.es_review_store import EsReviewStore
            store = EsReviewStore()
        except Exception as e:  # noqa: BLE001
            logger.warning("EsReviewStore 不可用,回退 sqlite: %s", e)
            store = SqliteReviewStore()
    else:
        if storage not in ("sqlite", ""):
            logger.warning("未知 review.storage=%r,回退 sqlite", storage)
        store = SqliteReviewStore()
    if cfg is None:
        _default_cache["singleton"] = store
    return store


# ---- 旧名兼容(过渡期/外部 import 仍可用)----
def _default_store() -> ReviewStore:
    return get_review_store()


def add_review(*args, **kwargs) -> dict:
    return _default_store().add(*args, **kwargs)


def get_reviews(data_file: str) -> list[dict]:
    return _default_store().get(data_file)


def delete_review(review_id) -> bool:
    return _default_store().delete(str(review_id))


def upsert_review(*args, **kwargs) -> dict:
    """Deprecated alias for add_review(允许多次评审,不再 upsert)。"""
    return _default_store().add(*args, **kwargs)
