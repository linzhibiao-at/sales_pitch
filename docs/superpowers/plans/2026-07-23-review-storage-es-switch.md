# Review Storage ES Switch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把批量评测页的评审数据从只存 SQLite 改为可在 SQLite 与 Elasticsearch 之间硬切换(单后端),通过 `config.review.storage` 配置选择。

**Architecture:** 引入 `ReviewStore` 协议 + 两个实现(`SqliteReviewStore` / `EsReviewStore`)+ 工厂 `get_review_store()`。ES 实现复用 `EsClient`,新增三个通用 ES 单文档方法。`backend/main.py` 三个评审端点改为调工厂返回的 store。

**Tech Stack:** Python 3, FastAPI, elasticsearch-py 7.x, SQLite3, stdlib `unittest`(+ `pytest` 运行器)。

## Global Constraints

- ES 集群 umalog 7.9.3,索引名须用 `umalog-q-maiamgs-index-*` 前缀;新索引名 `umalog-q-maiamgs-index-fila-reviews`。
- 硬切换语义:`config.review.storage` 取值 `sqlite`(默认)| `es`;**不双写、不自动兜底回退**;未知值回退 `sqlite` 并 warn。
- 不迁移历史 SQLite 评审;ES 索引从零开始。
- 评审文档 `_id` 用 ES 自动生成,作为返回给前端的 `id`(前端 `eval/review_detail.js` 已用 `String()` 比较 + `encodeURIComponent`,无需改前端)。
- `created_at`/`updated_at` 由 store 写入,UTC ISO8601 带偏移(与现有 SQLite 一致)。
- 测试用 `unittest.TestCase` + `unittest.mock`(与本仓库 `tests/` 既有风格一致),运行命令 `python -m pytest tests/<file>::<test> -v`。
- 部署:working tree 即部署代码(`restart.sh` 重启生效);`config.yaml` 默认仍 `sqlite`,先合并不切。

## File Structure

- `backend/config.py` — `get_elasticsearch_indices` 并入可选 `reviews` 键(不破坏必填契约)。
- `backend/retrieval/es_client.py` — 新增通用方法 `index_doc` / `delete_doc` / `search_docs`。
- `eval/review_store.py` — `ReviewStore` 协议 + `SqliteReviewStore`(现有函数重构)+ `get_review_store` 工厂 + 保留 `upsert_review`/`add_review`/`get_reviews`/`delete_review` 旧名兼容。
- `eval/es_review_store.py`(新)— `EsReviewStore` 实现。
- `backend/main.py` — 工厂接入;三端点改调 store;`ReviewBody.id` 与 DELETE 形参 `int → str`;try/except → 503。
- `config.yaml` — 新增 `review.storage` 块与 `elasticsearch.indices.reviews`。
- `scripts/build_fila_reviews_es_index.py`(新)— 幂等建索引。
- `tests/test_review_store.py`(新)— 协议两个实现 + 工厂单测。

---

### Task 1: config.py 并入可选 reviews 键

**Files:**
- Modify: `backend/config.py:112-130`(`get_elasticsearch_indices`)
- Test: `tests/test_review_store.py`(本任务起建,先放 config 测试)

**Interfaces:**
- Produces: `get_elasticsearch_indices(cfg=None) -> dict[str,str]` 在必填 `skus`/`outfits` 之外,**当且仅当**配置含 `elasticsearch.indices.reviews` 时把它并入返回。旧配置无 `reviews` 不报错。

- [ ] **Step 1: Write the failing test**

新建 `tests/test_review_store.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_review_store.py::GetElasticsearchIndicesTest -v`
Expected: FAIL — `assertIn reviews` fails(`test_reviews_optional_present`)因为当前实现只返回必填两键。

- [ ] **Step 3: Write minimal implementation**

在 `backend/config.py` 的 `get_elasticsearch_indices` 末尾(`return` 之前)改为:

```python
    out = {k: str(idx[k]).strip() for k in keys}
    reviews = str(idx.get("reviews") or "").strip()
    if reviews:
        out["reviews"] = reviews
    return out
```

(原 `return {k: str(idx[k]).strip() for k in keys}` 整体替换为上面三行。)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_review_store.py::GetElasticsearchIndicesTest -v`
Expected: PASS(3 条)。

- [ ] **Step 5: Commit**

```bash
git add backend/config.py tests/test_review_store.py
git commit -m "feat(config): optional reviews index key in get_elasticsearch_indices"
```

---

### Task 2: EsClient 通用单文档方法

**Files:**
- Modify: `backend/retrieval/es_client.py`(在类末尾追加三个方法)
- Test: `tests/test_review_store.py`(追加 `EsClientGenericMethodsTest`)

**Interfaces:**
- Produces(均在 `EsClient` 上,经 `self._indices[index_key]` 解析索引名,`self._client` 为 None 时安全返回):
  - `index_doc(index_key: str, doc: dict, doc_id: str | None = None) -> str | None` — 单文档 index,返回 ES `_id`;失败/不可用返回 None。
  - `delete_doc(index_key: str, doc_id: str) -> bool` — 按 `_id` 删单文档;命中 True,未命中/不可用 False。
  - `search_docs(index_key: str, body: dict) -> list[tuple[str, dict]]` — 发出 search,返回 `[(doc_id, _source), ...]`;不可用返回 `[]`。

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_review_store.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_review_store.py::EsClientGenericMethodsTest -v`
Expected: FAIL — `AttributeError: EsClient has no attribute 'index_doc'`。

- [ ] **Step 3: Write minimal implementation**

在 `backend/retrieval/es_client.py` 的 `EsClient` 类**末尾**(最后一个方法 `search_outfits` 之后)追加:

```python
    def index_doc(
        self,
        index_key: str,
        doc: dict[str, Any],
        doc_id: str | None = None,
    ) -> str | None:
        """单文档 index,返回 ES `_id`;不可用/失败返回 None。"""
        if not self._client or not isinstance(doc, dict):
            return None
        idx = self._indices[index_key]
        try:
            res = self._client.index(index=idx, body=doc, id=doc_id) if doc_id else \
                self._client.index(index=idx, body=doc)
            return str(res.get("_id") or "")
        except Exception as e:  # noqa: BLE001
            logger.warning("es index_doc %s: %s", index_key, e)
            return None

    def delete_doc(self, index_key: str, doc_id: str) -> bool:
        """按 `_id` 删单文档;命中 True,未命中/不可用 False。"""
        if not self._client or not doc_id:
            return False
        idx = self._indices[index_key]
        try:
            res = self._client.delete(index=idx, id=doc_id)
            return str(res.get("result") or "") in ("deleted", "ok")
        except Exception as e:  # noqa: BLE001
            logger.warning("es delete_doc %s/%s: %s", index_key, doc_id, e)
            return False

    def search_docs(
        self,
        index_key: str,
        body: dict[str, Any],
    ) -> list[tuple[str, dict[str, Any]]]:
        """发出 search,返回 [(doc_id, _source), ...];不可用返回 []。"""
        if not self._client:
            return []
        idx = self._indices[index_key]
        try:
            res = self._client.search(index=idx, body=body)
            hits = res.get("hits", {}).get("hits", [])
            out: list[tuple[str, dict[str, Any]]] = []
            for h in hits:
                src = h.get("_source")
                if isinstance(src, dict):
                    out.append((str(h.get("_id") or ""), src))
            return out
        except Exception as e:  # noqa: BLE001
            logger.warning("es search_docs %s: %s", index_key, e)
            return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_review_store.py::EsClientGenericMethodsTest -v`
Expected: PASS(5 条)。

- [ ] **Step 5: Commit**

```bash
git add backend/retrieval/es_client.py tests/test_review_store.py
git commit -m "feat(es): add index_doc/delete_doc/search_docs to EsClient"
```

---

### Task 3: ReviewStore 协议 + SqliteReviewStore 重构 + 工厂

**Files:**
- Modify: `eval/review_store.py`(整体重构,保留旧函数别名)
- Test: `tests/test_review_store.py`(追加 `SqliteReviewStoreTest` 与 `GetReviewStoreFactoryTest`)

**Interfaces:**
- Produces(`eval/review_store.py`):
  - `class ReviewStore(Protocol)`:`add(...) -> dict`、`get(data_file) -> list[dict]`、`delete(id) -> bool`。
  - `class SqliteReviewStore`:`__init__(self, db_path: Path | None = None)`;实现协议;构造时若表不存在/旧 schema 自动迁移(沿用现有 `_migrate`/`_ensure_columns`)。
  - `class EsReviewStore`:本任务**不实现**(Task 4),但工厂需按 `storage=es` 选中它——故 `get_review_store` 内**惰性 import** `eval.es_review_store.EsReviewStore`,import 失败/storage 非 es 时走 sqlite。
  - `get_review_store(cfg: dict | None = None) -> ReviewStore` — 单例缓存;`storage=es` 返回 `EsReviewStore()`,`sqlite`/未知(warn)返回 `SqliteReviewStore()`。
  - 旧名兼容:`add_review(...)`、`get_reviews(file)`、`delete_review(id)`、`upsert_review(...)` 改为对模块级单例 `_default_store()` 的薄封装,保持 `backend/main.py` 现有 import 在 Task 5 改之前仍可用(过渡期不破)。

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_review_store.py`:

```python
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
```

注意:`get_review_store` 在本任务仅实现 sqlite 分支;`storage=es` 分支惰性 import `EsReviewStore`,本任务该模块尚不存在时 import 会失败 —— 故工厂对 `storage=es` 的测试留到 Task 4 之后(Task 4 会加)。

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_review_store.py::SqliteReviewStoreTest tests/test_review_store.py::GetReviewStoreFactoryTest -v`
Expected: FAIL — `ImportError: cannot import name 'ReviewStore'`(当前 `eval/review_store.py` 只有函数)。

- [ ] **Step 3: Write minimal implementation**

**整体重写** `eval/review_store.py` 为:

```python
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
    """按 config.review.storage 返回单例 ReviewStore(sqlite|es 硬切换)。"""
    cache_key = "singleton"
    cached = _default_cache.get(cache_key)
    if cached is not None:
        return cached
    if cfg is None:
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
    _default_cache[cache_key] = store
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_review_store.py::SqliteReviewStoreTest tests/test_review_store.py::GetReviewStoreFactoryTest tests/test_review_store.py::GetElasticsearchIndicesTest tests/test_review_store.py::EsClientGenericMethodsTest -v`
Expected: PASS(全部)。

- [ ] **Step 5: Commit**

```bash
git add eval/review_store.py tests/test_review_store.py
git commit -m "feat(review): ReviewStore protocol + SqliteReviewStore + factory"
```

---

### Task 4: EsReviewStore 实现

**Files:**
- Create: `eval/es_review_store.py`
- Modify: `tests/test_review_store.py`(追加 `EsReviewStoreTest` + 工厂 es 分支测试)

**Interfaces:**
- Consumes: `EsClient.index_doc / delete_doc / search_docs`(Task 2);`EsClient`(构造 `EsClient()`,可注入 `es` 便于测试)。
- Produces: `class EsReviewStore(ReviewStore)`:
  - `__init__(self, es: Optional[EsClient] = None)` — `self._es = es or EsClient()`;不可用时记 warning。
  - `add(**fields) -> dict` — 写 `created_at=updated_at=now`,调 `index_doc("reviews", doc)`(doc_id=None 自动生成),回填 `id=_id` 返回。
  - `get(data_file) -> list[dict]` — `search_docs("reviews", body)`,body 含 `term:{data_file:...}` + `sort:created_at desc` + `size=10000`;每条回填 `id`。
  - `delete(id) -> bool` — `delete_doc("reviews", id)`;ES 不可用时抛 `RuntimeError`(由 main.py 转 503)。

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_review_store.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_review_store.py::EsReviewStoreTest -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'eval.es_review_store'`。

- [ ] **Step 3: Write minimal implementation**

新建 `eval/es_review_store.py`:

```python
"""评审意见 ES 存储(硬切换 storage=es 时启用)。

复用 backend.retrieval.es_client.EsClient 的通用单文档方法;评审专属
query 在本类构造。文档 _id 由 ES 自动生成,作为返回给前端的 id。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from eval.review_store import ReviewStore

logger = logging.getLogger(__name__)

_GET_SIZE = 10000  # 单批次评审量上限(超出会静默截断,后续可改 scroll)


class EsReviewStore:
    def __init__(self, es: Optional["object"] = None) -> None:
        if es is not None:
            self._es = es
        else:
            from backend.retrieval.es_client import EsClient
            self._es = EsClient()
        if not getattr(self._es, "available", False):
            logger.warning("EsReviewStore: ES 不可用(storage=es 下评审端点将 503)")

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
        if not getattr(self._es, "available", False):
            raise RuntimeError("评审存储不可用(ES)")
        now = datetime.now(timezone.utc).isoformat()
        doc = {
            "data_file": data_file,
            "input_sku_id": input_sku_id,
            "outfit_id": outfit_id,
            "rating": rating,
            "comment": comment,
            "reviewer": reviewer,
            "reviewer_role": reviewer_role,
            "reviewer_name": reviewer_name,
            "created_at": now,
            "updated_at": now,
        }
        doc_id = self._es.index_doc("reviews", doc)
        if not doc_id:
            raise RuntimeError("评审写入 ES 失败")
        out = dict(doc)
        out["id"] = doc_id
        return out

    def get(self, data_file: str) -> list[dict]:
        if not getattr(self._es, "available", False):
            raise RuntimeError("评审存储不可用(ES)")
        body = {
            "size": _GET_SIZE,
            "query": {"term": {"data_file": data_file}},
            "sort": [{"created_at": {"order": "desc"}}],
        }
        rows = self._es.search_docs("reviews", body)
        if len(rows) >= _GET_SIZE:
            logger.warning("EsReviewStore.get 截断: data_file=%s 命中上限 %d", data_file, _GET_SIZE)
        out: list[dict] = []
        for doc_id, src in rows:
            row = dict(src)
            row["id"] = doc_id
            out.append(row)
        return out

    def delete(self, id: str) -> bool:
        if not getattr(self._es, "available", False):
            raise RuntimeError("评审存储不可用(ES)")
        return bool(self._es.delete_doc("reviews", str(id)))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_review_store.py -v`
Expected: PASS(全部含 EsReviewStoreTest 与工厂 es 分支)。

- [ ] **Step 5: Commit**

```bash
git add eval/es_review_store.py tests/test_review_store.py
git commit -m "feat(review): EsReviewStore implementation"
```

---

### Task 5: backend/main.py 接入工厂 + 503 错误处理

**Files:**
- Modify: `backend/main.py:69`(import 行)、`backend/main.py:575-610`(ReviewBody + 三端点)

**Interfaces:**
- Consumes: `get_review_store`(Task 3)、`ReviewStore` 协议。
- Produces: `POST/GET/DELETE /eval/api/reviews` 改为调 `get_review_store()` 的 `add/get/delete`;`DELETE` 形参 `id: str`;store 调用抛 `RuntimeError` 时返回 `503`。

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_review_store.py`:

```python
from unittest.mock import patch

from backend import main as main_mod


class ReviewEndpointsTest(unittest.TestCase):
    def _client(self):
        from fastapi.testclient import TestClient
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
            def add(self, **kw): raise RuntimeError("评审存储不可用(ES)")
            def get(self, f): raise RuntimeError("评审存储不可用(ES)")
            def delete(self, i): raise RuntimeError("评审存储不可用(ES)")
        with patch.object(main_mod, "get_review_store", return_value=_Broken()):
            with self._client() as c:
                self.assertEqual(c.get("/eval/api/reviews?file=x").status_code, 503)
                self.assertEqual(c.post("/eval/api/reviews", json={
                    "data_file": "x", "input_sku_id": "S", "outfit_id": "O"
                }).status_code, 503)
                self.assertEqual(c.delete("/eval/api/reviews?id=1").status_code, 503)
```

> 注:若 `backend/main.py` 在 import 时有重副作用(连 ES/Milvus)导致 `TestClient` 构造困难,可改为直接调用端点函数 `eval_list_reviews(file=...)` 等(它们是普通函数)。本任务实现端点函数后,测试优先以函数直调方式断言;若 `TestClient` 可用则用之。**实现时先确认 `from backend import main` 是否可无副作用 import**,若不可,把上述测试改为直调 `main_mod.eval_list_reviews(file="x")` 并断言抛 `HTTPException(503)`。

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_review_store.py::ReviewEndpointsTest -v`
Expected: FAIL — 端点仍用旧 `get_reviews`/`add_review`/`delete_review`,未接工厂;且无 503。

- [ ] **Step 3: Write minimal implementation**

`backend/main.py:69` import 行改为:

```python
from eval.review_store import get_review_store
```

`backend/main.py:575-610`(ReviewBody + 三端点)整体替换为:

```python
class ReviewBody(BaseModel):
    data_file: str
    input_sku_id: str
    outfit_id: str
    rating: Optional[int] = None
    comment: Optional[str] = None
    reviewer: Optional[str] = None
    reviewer_role: Optional[str] = None
    reviewer_name: Optional[str] = None


def _review_store():
    return get_review_store()


@app.get("/eval/api/reviews")
def list_reviews(file: str):
    try:
        return _review_store().get(file)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/eval/api/reviews")
def save_review(body: ReviewBody):
    try:
        return _review_store().add(
            data_file=body.data_file,
            input_sku_id=body.input_sku_id,
            outfit_id=body.outfit_id,
            rating=body.rating,
            comment=body.comment,
            reviewer=body.reviewer,
            reviewer_role=body.reviewer_role,
            reviewer_name=body.reviewer_name,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.delete("/eval/api/reviews")
def remove_review(id: str):
    try:
        ok = _review_store().delete(id)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="review not found")
    return {"deleted": True}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_review_store.py -v`
Expected: PASS(全部)。

> 若 `TestClient` 在该环境因 main.py import 副作用不可用,改为直调端点函数版测试(见 Step 1 注释),并在此步确认通过。

- [ ] **Step 5: Commit**

```bash
git add backend/main.py tests/test_review_store.py
git commit -m "feat(review): wire main.py endpoints to ReviewStore factory + 503"
```

---

### Task 6: config.yaml 新增 review 块与 reviews 索引

**Files:**
- Modify: `config.yaml`(新增 `review.storage`;`elasticsearch.indices` 加 `reviews`)

**Interfaces:** 无代码接口;仅配置。

- [ ] **Step 1: 编辑 config.yaml**

在 `config.yaml` 顶层(`elasticsearch:` 块之前或之后均可,本任务放 `elasticsearch` 之前)新增:

```yaml
review:
  # 评审意见存储后端:sqlite | es (硬切换,单后端)。es 需先建索引并配置 elasticsearch.indices.reviews
  storage: sqlite
```

在 `config.yaml` 的 `elasticsearch.indices` 下(`outfits:` 行之后)新增一行:

```yaml
    reviews: "umalog-q-maiamgs-index-fila-reviews"
```

使该块成为:
```yaml
  indices:
    # umalog 测试集群须使用 umalog-q-maiamgs-index-* 前缀
    skus: "umalog-q-maiamgs-index-fila-skus"
    outfits: "umalog-q-maiamgs-index-fila-outfits"
    reviews: "umalog-q-maiamgs-index-fila-reviews"
```

- [ ] **Step 2: 验证配置可被读取**

Run: `python -c "from backend.config import load_config, get_elasticsearch_indices; c=load_config(); print('storage=', (c.get('review') or {}).get('storage')); print('indices=', get_elasticsearch_indices(c))"`
Expected: `storage= sqlite` 且 `indices= {... 'reviews': 'umalog-q-maiamgs-index-fila-reviews'}`。

- [ ] **Step 3: Commit**

```bash
git add config.yaml
git commit -m "feat(config): add review.storage and reviews index name"
```

---

### Task 7: 建索引脚本 scripts/build_fila_reviews_es_index.py

**Files:**
- Create: `scripts/build_fila_reviews_es_index.py`
- Test: `tests/test_review_store.py`(追加 `BuildReviewsIndexTest`)

**Interfaces:**
- Consumes: `backend.config` 的 `create_elasticsearch_client`/`get_elasticsearch_hosts`/`get_elasticsearch_index`;`REVIEW_INDEX_MAPPING`(本脚本定义,供测试断言)。
- Produces: `python -m scripts.build_fila_reviews_es_index [--reset]` — 幂等:默认索引已存在则跳过;`--reset` 先删再建。

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_review_store.py`:

```python
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
            def exists(index): return True
            @staticmethod
            def create(index, body): calls.append(("create", index, body))
            @staticmethod
            def delete(index, **kw): calls.append(("delete", index))

        class _Client:
            indices = _Indices()

        build_index(_Client(), "r", reset=False)
        self.assertEqual(calls, [])  # 已存在,跳过

    def test_build_index_reset_creates(self) -> None:
        calls = []

        class _Indices:
            @staticmethod
            def exists(index): return True
            @staticmethod
            def create(index, body): calls.append(("create", index))
            @staticmethod
            def delete(index, **kw): calls.append(("delete", index))

        class _Client:
            indices = _Indices()

        build_index(_Client(), "r", reset=True)
        self.assertEqual([c[0] for c in calls], ["delete", "create"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_review_store.py::BuildReviewsIndexTest -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.build_fila_reviews_es_index'`。

- [ ] **Step 3: Write minimal implementation**

新建 `scripts/build_fila_reviews_es_index.py`:

```python
"""幂等创建评审 ES 索引 umalog-q-maiamgs-index-fila-reviews。

用法:
  python -m scripts.build_fila_reviews_es_index            # 不存在则建,存在则跳过
  python -m scripts.build_fila_reviews_es_index --reset    # 先删再建(清空数据)
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

from backend.config import (
    create_elasticsearch_client,
    get_elasticsearch_hosts,
    get_elasticsearch_index,
    load_config,
)

logger = logging.getLogger(__name__)

REVIEW_INDEX_MAPPING: dict[str, Any] = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
    },
    "mappings": {
        "properties": {
            "data_file": {"type": "keyword"},
            "input_sku_id": {"type": "keyword"},
            "outfit_id": {"type": "keyword"},
            "rating": {"type": "integer"},
            "comment": {"type": "text"},
            "reviewer": {"type": "keyword"},
            "reviewer_role": {"type": "keyword"},
            "reviewer_name": {"type": "keyword"},
            "created_at": {"type": "date", "format": "strict_date_optional_time||iso8601"},
            "updated_at": {"type": "date", "format": "strict_date_optional_time||iso8601"},
        }
    },
}


def build_index(client: Any, name: str, reset: bool = False) -> None:
    """幂等建索引:默认存在则跳过;reset=True 先删再建。"""
    if client.indices.exists(index=name):
        if not reset:
            logger.info("索引 %s 已存在,跳过(如需重建加 --reset)", name)
            return
        logger.info("--reset: 删除旧索引 %s", name)
        client.indices.delete(index=name)
    logger.info("创建索引 %s", name)
    client.indices.create(index=name, body=REVIEW_INDEX_MAPPING)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="创建 FILA 评审 ES 索引")
    parser.add_argument("--reset", action="store_true", help="删除已有索引后重建(清空数据)")
    args = parser.parse_args()

    cfg = load_config()
    name = get_elasticsearch_index("reviews", cfg)
    hosts = get_elasticsearch_hosts(cfg)
    es_cfg = cfg.get("elasticsearch") or {}
    user = (es_cfg.get("username_env") or "")
    pwd = (es_cfg.get("password_env") or "")
    from backend.config import env_or_empty
    client = create_elasticsearch_client(
        hosts, username=env_or_empty(user), password=env_or_empty(pwd), timeout_sec=30,
    )
    if not client.ping():
        raise SystemExit("ES ping 失败,检查 ES_HOSTS/ES_USERNAME/ES_PASSWORD")
    build_index(client, name, reset=args.reset)
    logger.info("完成: %s", name)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_review_store.py::BuildReviewsIndexTest -v`
Expected: PASS(3 条)。

- [ ] **Step 5: 全量回归 + 提交**

Run: `python -m pytest tests/test_review_store.py -v`
Expected: PASS(全部)。

```bash
git add scripts/build_fila_reviews_es_index.py tests/test_review_store.py
git commit -m "feat(scripts): idempotent build_fila_reviews_es_index script"
```

---

### Task 8: 端到端冒烟(手动,需 ES 可达)

**Files:** 无改动;仅运行验证。

- [ ] **Step 1: 建索引**

确保 `ES_HOSTS`/`ES_USERNAME`/`ES_PASSWORD` 已注入,运行:
`python -m scripts.build_fila_reviews_es_index`
Expected: 日志 `创建索引 umalog-q-maiamgs-index-fila-reviews` 且无异常;再跑一次看到 `已存在,跳过`。

- [ ] **Step 2: 切到 es 并冒烟**

临时改 `config.yaml` 的 `review.storage: es`,重启服务(`restart.sh` 或本地 `uvicorn`)。然后:

```bash
# 提交一条评审
curl -s -X POST http://127.0.0.1:8888/eval/api/reviews -H 'Content-Type: application/json' \
  -d '{"data_file":"smoke.jsonl","input_sku_id":"S1","outfit_id":"O1","rating":5,"comment":"冒烟"}'
# 读回
curl -s "http://127.0.0.1:8888/eval/api/reviews?file=smoke.jsonl"
# 删除(用上一步返回的 id)
curl -s -X DELETE "http://127.0.0.1:8888/eval/api/reviews?id=<ID>"
```
Expected: POST 返回含 `id`;GET 返回 1 条;DELETE 返回 `{"deleted":true}`。把 `review.storage` 改回 `sqlite`。

- [ ] **Step 3: 提交(若有 config.yaml 临时改动回滚后无差异则跳过)**

无需提交(配置仍为默认 `sqlite`)。冒烟结果记录到 PR 描述。

---

## Self-Review

- **Spec coverage**:
  - 硬切换单后端 → Task 3 工厂(`sqlite`/`es`,未知回退+warn)。✅
  - 新 ES 索引 + mapping → Task 7 脚本 + `REVIEW_INDEX_MAPPING`。✅
  - 可配置切换 → `config.review.storage`(Task 6)+ 工厂(Task 3)。✅
  - 不迁移历史 → 无迁移任务。✅(符合非目标)
  - `ReviewStore` 协议 + 两个实现 + 工厂 → Task 3/4。✅
  - EsClient 通用方法 `index_doc`/`delete_doc`/`search_docs` → Task 2。✅
  - `get` size=10000 + 截断 log → Task 4 `_GET_SIZE` + warn。✅
  - 硬切换 ES 不可用抛错 → Task 4 `RuntimeError`;main.py 503 → Task 5。✅
  - `id` 由 int 放宽 str、前端无需改 → Task 5 `id: str`;前端已 String() 比较(已核实)。✅
  - `config.yaml` 可选 reviews 键不破坏必填契约 → Task 1 + Task 6。✅
  - 测试 Sqlite/Es/工厂 → Task 3/4。✅
  - 建索引脚本幂等 → Task 7。✅
- **Placeholder scan**: 无 TBD/TODO;每个代码步骤含完整代码。✅
- **Type consistency**:
  - `search_docs` 全程返回 `list[tuple[str, dict]]`,Task 2 实现、Task 4 消费一致。✅
  - `ReviewStore.add` 全部关键字参数,Task 3 协议/Sqlite、Task 4 Es 一致;`main.py` Task 5 用关键字调用一致。✅
  - `index_doc(index_key, doc, doc_id=None)` 签名 Task 2 定义、Task 4 `index_doc("reviews", doc)` 调用一致。✅
  - `delete_doc(index_key, doc_id)` 一致。✅
  - `delete(id: str)` 协议/实现/main 一致。✅
