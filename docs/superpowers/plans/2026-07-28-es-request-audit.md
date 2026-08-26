# ES 请求审计落库 + 审计展示页 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把每次对外 `/v1/outfit/recommend` 与 `/v1/outfit/regenerate-reason` 请求的输入/意图/召回/结果落库到新 ES 索引 `fila-requests`，并新增只读审计展示页。

**Architecture:** 在 `RecommendService.external_recommend` 遍历 `chat_stream` 时采集已有 SSE 事件载荷（零额外计算），循环结束拼一条审计文档经 `EsClient.index_doc` 写 ES（`refresh=False`，失败只告警不连坐）。`external_regenerate` 同理写一条 `request_kind=regenerate_reason` 文档。新增 `RequestAuditLogger` + 纯函数 `build_*_doc` 便于单测。前端 `web/audit.html` 经两个只读 API 查询 ES。

**Tech Stack:** Python 3 / FastAPI / elasticsearch-py 7.x（项目固定 7.x，ES 7.9）/ pytest / 原生 HTML+JS（复用 `/web/styles.css`）。

## Global Constraints

- ES 客户端固定 `elasticsearch>=7.17.0,<8`，`create_elasticsearch_client` 已封装；新索引名遵循 `umalog-q-maiamgs-index-fila-*` 前缀。
- 所有 ES 写/读均经 `backend/retrieval/es_client.py:EsClient`；不可用/失败必须静默降级（返 None / [] / 空列表），绝不 raise 到业务。
- 图片只存 `image_url` + 抓取字节 `sha1`，不存 base64 原文。
- `chat_stream` 公共流程不动（内部 `/chat` 复用），审计采集只发生在 `external_recommend`。
- 模型层字段已在 `ExternalRecommendRequest` / `ExternalRegenerateReasonRequest` 完成 surrogate/HTML 清洗，审计直接用入参原值。
- 测试风格沿用 `tests/test_review_store.py`：`unittest.TestCase` + `_make_es(fake_client, indices)` + `_FakeEsClient`。

---

## File Structure

- **Create** `backend/services/request_audit.py` — `RequestAuditLogger` + 纯函数 `build_input_block / build_recommend_doc / build_regenerate_doc / build_audit_search_body / slim_audit_row / now_iso`。
- **Modify** `backend/config.py` — `get_elasticsearch_indices` 增 `requests` 可选键；新增 `get_request_audit_enabled`。
- **Modify** `backend/retrieval/es_client.py` — `index_doc` 加 `refresh` 形参 + 未知 key 防御；`search_docs` 加未知 key 防御。
- **Modify** `backend/services/recommend_service.py` — `__init__` 装 `_audit`；`external_recommend` 采集事件并写审计；`external_regenerate` 写审计；新增 `_write_recommend_audit` / `_write_regenerate_audit`。
- **Modify** `backend/main.py` — 两个 v1 端点传入 `trace_id/app_id/caller`；新增 `GET /api/audit/requests` 与 `GET /api/audit/requests/{trace_id}`。
- **Modify** `config.yaml` — `elasticsearch.indices.requests` + `elasticsearch.request_audit.enabled`。
- **Create** `web/audit.html` + `web/audit.js`；**Modify** `web/index.html` 加导航入口。
- **Create** `tests/test_request_audit.py`。

---

### Task 1: Config — `requests` 索引键 + 审计开关

**Files:**
- Modify: `backend/config.py`（`get_elasticsearch_indices` 与新增 `get_request_audit_enabled`）
- Modify: `config.yaml`（`elasticsearch.indices` 与 `elasticsearch.request_audit`）
- Test: `tests/test_request_audit.py`（config 部分）

**Interfaces:**
- Produces: `get_elasticsearch_indices(cfg=None) -> dict[str,str]`（新增可选 `requests` 键，与 `reviews` 同策略）；`get_request_audit_enabled(cfg=None) -> bool`（缺省 True）。

- [ ] **Step 1: Write the failing test**

`tests/test_request_audit.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_request_audit.py -v`
Expected: FAIL — `get_request_audit_enabled` 不存在 / `requests` 键未识别。

- [ ] **Step 3: Write minimal implementation**

在 `backend/config.py` 的 `get_elasticsearch_indices` 中，紧接 `reviews` 可选块之后追加 `requests` 可选块：

```python
    out = {k: str(idx[k]).strip() for k in keys}
    reviews = str(idx.get("reviews") or "").strip()
    if reviews:
        out["reviews"] = reviews
    requests = str(idx.get("requests") or "").strip()
    if requests:
        out["requests"] = requests
    return out
```

并在该函数定义之后新增：

```python
def get_request_audit_enabled(cfg: Optional[dict[str, Any]] = None) -> bool:
    """对外请求审计落库开关（elasticsearch.request_audit.enabled，缺省 True）。"""
    data = cfg if cfg is not None else load_config()
    es = data.get("elasticsearch") or {}
    ra = es.get("request_audit") or {}
    return bool(ra.get("enabled", True))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_request_audit.py -v`
Expected: PASS（4 用例全过）。

- [ ] **Step 5: Update config.yaml**

在 `config.yaml` 的 `elasticsearch.indices` 块加 `requests`，并在 `elasticsearch` 下加 `request_audit` 块：

```yaml
  indices:
    # umalog 测试集群须使用 umalog-q-maiamgs-index-* 前缀
    skus: "umalog-q-maiamgs-index-fila-skus"
    outfits: "umalog-q-maiamgs-index-fila-outfits"
    reviews: "umalog-q-maiamgs-index-fila-reviews"
    # 对外请求审计落库（输入/意图/召回/结果），需预先在该集群创建
    requests: "umalog-q-maiamgs-index-fila-requests"
  # 对外请求审计开关：false 时既不写也不查（查询 API 返空/503）
  request_audit:
    enabled: true
```

- [ ] **Step 6: Commit**

```bash
git add backend/config.py config.yaml tests/test_request_audit.py
git commit -m "feat(audit): config 增加 requests 索引键与 request_audit 开关"
```

---

### Task 2: EsClient — `index_doc` refresh 形参与未知 key 防御

**Files:**
- Modify: `backend/retrieval/es_client.py`（`index_doc` 与 `search_docs`）
- Test: `tests/test_request_audit.py`（EsClient 部分；复用 `test_review_store._make_es` 模式）

**Interfaces:**
- Produces: `EsClient.index_doc(index_key, doc, doc_id=None, refresh=True) -> str|None`（新增 `refresh` 形参；未知 `index_key` 返 None）；`EsClient.search_docs(index_key, body)`（未知 `index_key` 返 []）。

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_request_audit.py`：

```python
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
        return {"hits": {"hits": [
            {"_id": i, "_source": dict(s)} for i, s in self.docs.items()
        ]}}


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_request_audit.py -v`
Expected: FAIL — `index_doc` 无 `refresh` 形参 / 未知 key 抛 KeyError。

- [ ] **Step 3: Write minimal implementation**

替换 `backend/retrieval/es_client.py` 的 `index_doc` 整个方法：

```python
    def index_doc(
        self,
        index_key: str,
        doc: dict[str, Any],
        doc_id: str | None = None,
        refresh: bool = True,
    ) -> str | None:
        """单文档 index,返回 ES `_id`;不可用/失败/未知索引返回 None。"""
        if not self._client or not isinstance(doc, dict):
            return None
        if index_key not in self._indices:
            return None
        idx = self._indices[index_key]
        try:
            if doc_id:
                res = self._client.index(
                    index=idx, body=doc, id=doc_id, refresh=refresh,
                )
            else:
                res = self._client.index(index=idx, body=doc, refresh=refresh)
            return str(res.get("_id") or "")
        except Exception as e:  # noqa: BLE001
            logger.warning("es index_doc %s: %s", index_key, e)
            return None
```

在 `search_docs` 方法开头加未知 key 防御（`if not self._client:` 之后、取 `idx` 之前）：

```python
    def search_docs(
        self,
        index_key: str,
        body: dict[str, Any],
    ) -> list[tuple[str, dict[str, Any]]]:
        """发出 search,返回 [(doc_id, _source), ...];不可用/未知索引返回 []。"""
        if not self._client:
            return []
        if index_key not in self._indices:
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

Run: `python3 -m pytest tests/test_request_audit.py -v`
Expected: PASS（全部用例）。

- [ ] **Step 5: Run existing review-store tests to ensure no regression**

Run: `python3 -m pytest tests/test_review_store.py -v`
Expected: PASS（`index_doc` 默认 `refresh=True` 兼容旧调用）。

- [ ] **Step 6: Commit**

```bash
git add backend/retrieval/es_client.py tests/test_request_audit.py
git commit -m "feat(es): index_doc 支持 refresh 形参与未知索引防御"
```

---

### Task 3: RequestAuditLogger + 纯函数 build_*_doc

**Files:**
- Create: `backend/services/request_audit.py`
- Test: `tests/test_request_audit.py`（追加 audit 模块用例）

**Interfaces:**
- Consumes: `backend.config.get_request_audit_enabled`、`backend.retrieval.es_client.EsClient.index_doc`（`index_key="requests"`）。
- Produces:
  - `now_iso() -> str`
  - `build_input_block(*, input_sku_id="", image_url=None, image_base64=None, message=None, tryon=False, reason_style=None, outfit_id=None) -> dict`
  - `build_recommend_doc(*, input_block, captured, meta) -> dict`（`captured` 形如 `{"intent":ev,"anchor_skus":ev,"recall_done":ev,"recall_progress":[ev,...],"ranking_reason_done":ev,"outfits":[reshaped_outfits]}`；`meta` 形如 `{"trace_id","session_id","app_id","caller","ts","elapsed_ms","status","error"}`）
  - `build_regenerate_doc(*, input_block, result, meta) -> dict`（`result` 为 `{"outfit_id","reason","error"?}` 或 None）
  - `build_audit_search_body(filters: dict) -> dict`、`slim_audit_row(src: dict) -> dict`
  - `RequestAuditLogger(es=None, enabled=None)`，属性 `enabled`，方法 `write(doc) -> None`、`search(body) -> list[(id,src)]`、`get_by_trace_id(trace_id) -> dict|None`

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_request_audit.py`：

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_request_audit.py -v`
Expected: FAIL — 模块/函数不存在。

- [ ] **Step 3: Write minimal implementation**

Create `backend/services/request_audit.py`:

```python
"""对外请求审计落库到 ES（fila-requests 索引）。

纯函数 build_*_doc 便于单测；RequestAuditLogger 负责写/查，失败静默降级。
"""

from __future__ import annotations

import base64
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from backend.config import get_request_audit_enabled

logger = logging.getLogger(__name__)


def now_iso() -> str:
    """UTC + 本地时区 iso 字符串（与 jsonl_logger 口径一致）。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def build_input_block(
    *,
    input_sku_id: str = "",
    image_url: Optional[str] = None,
    image_base64: Optional[str] = None,
    message: Optional[str] = None,
    tryon: bool = False,
    reason_style: Optional[str] = None,
    outfit_id: Optional[str] = None,
) -> dict[str, Any]:
    """构造审计文档的 input 子结构；图片只存 url + 抓取字节 sha1。"""
    image_sha1: Optional[str] = None
    if image_base64:
        try:
            image_sha1 = hashlib.sha1(base64.b64decode(image_base64)).hexdigest()
        except Exception:  # noqa: BLE001
            image_sha1 = None
    return {
        "input_sku_id": input_sku_id or "",
        "image_url": image_url or None,
        "image_sha1": image_sha1,
        "message": message or None,
        "tryon": bool(tryon),
        "reason_style": reason_style or None,
        "outfit_id": outfit_id or None,
    }


def _slim_outfits(outfits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for card in outfits or []:
        items = []
        for it in card.get("items") or []:
            items.append({
                "sku_id": it.get("sku_id"),
                "role": it.get("role"),
                "title": it.get("title"),
                "spu_id": it.get("spu_id"),
                "id_goods": it.get("id_goods"),
            })
        out.append({
            "outfit_id": card.get("outfit_id"),
            "outfit_rank": card.get("outfit_rank"),
            "reason": card.get("reason"),
            "items": items,
        })
    return out


def build_recommend_doc(
    *,
    input_block: dict[str, Any],
    captured: dict[str, Any],
    meta: dict[str, Any],
) -> dict[str, Any]:
    """拼 recommend 审计文档。captured 为 chat_stream 采集的事件集合。"""
    intent_ev = captured.get("intent") or {}
    intent_block = None
    if intent_ev:
        intent_block = {
            "intent": intent_ev.get("intent"),
            "method": intent_ev.get("method"),
            "confidence": intent_ev.get("confidence"),
            "llm_fallback": intent_ev.get("llm_fallback"),
            "image_override": intent_ev.get("image_override"),
            "anchor_source": intent_ev.get("anchor_source"),
            "image_role": intent_ev.get("image_role"),
        }

    recall_ev = captured.get("recall_done") or {}
    anchor_skus = (captured.get("anchor_skus") or {}).get("skus") or []
    anchor_sku_id = anchor_skus[0].get("sku_id") if anchor_skus else None
    paths: dict[str, Any] = {}
    for pe in captured.get("recall_progress") or []:
        pname = pe.get("path")
        if pname:
            paths[pname] = {
                "count": pe.get("count", 0),
                "elapsed_ms": pe.get("elapsed_ms", 0),
            }
    recall_block = None
    if recall_ev:
        recall_block = {
            "anchor_sku_id": anchor_sku_id,
            "mode": recall_ev.get("mode"),
            "recalled_sku_count": recall_ev.get("recalled_sku_count", 0),
            "composed_outfit_count": recall_ev.get("composed_outfit_count", 0),
            "before_dedupe": recall_ev.get("before_dedupe", 0),
            "after_dedupe": recall_ev.get("after_dedupe", 0),
            "paths": paths,
            "roles": recall_ev.get("roles") or {},
        }

    ranking_ev = captured.get("ranking_reason_done") or {}
    ranking_block = None
    if ranking_ev:
        ranking_block = {
            "input_count": ranking_ev.get("input_count", 0),
            "output_count": ranking_ev.get("output_count", 0),
            "scoring_method": ranking_ev.get("scoring_method"),
            "ranking_elapsed_ms": ranking_ev.get("ranking_elapsed_ms", 0),
        }

    return {
        "trace_id": meta.get("trace_id"),
        "session_id": meta.get("session_id"),
        "app_id": meta.get("app_id"),
        "caller": meta.get("caller"),
        "request_kind": "recommend",
        "ts": meta.get("ts"),
        "elapsed_ms": meta.get("elapsed_ms"),
        "status": meta.get("status", "ok"),
        "error": meta.get("error"),
        "input": input_block,
        "intent": intent_block,
        "recall": recall_block,
        "ranking": ranking_block,
        "result": {"outfits": _slim_outfits(captured.get("outfits") or [])},
    }


def build_regenerate_doc(
    *,
    input_block: dict[str, Any],
    result: Optional[dict[str, Any]],
    meta: dict[str, Any],
) -> dict[str, Any]:
    """拼 regenerate_reason 审计文档；intent/recall/ranking 不适用，置 None。"""
    res_block: Optional[dict[str, Any]] = None
    if isinstance(result, dict):
        if "error" in result:
            res_block = {
                "outfit_id": result.get("outfit_id"),
                "reason": None,
                "error": result.get("error"),
            }
        else:
            res_block = {
                "outfit_id": result.get("outfit_id"),
                "reason": result.get("reason"),
            }
    return {
        "trace_id": meta.get("trace_id"),
        "session_id": meta.get("session_id"),
        "app_id": meta.get("app_id"),
        "caller": meta.get("caller"),
        "request_kind": "regenerate_reason",
        "ts": meta.get("ts"),
        "elapsed_ms": meta.get("elapsed_ms"),
        "status": meta.get("status", "ok"),
        "error": meta.get("error"),
        "input": input_block,
        "intent": None,
        "recall": None,
        "ranking": None,
        "result": res_block,
    }


def build_audit_search_body(filters: dict[str, Any]) -> dict[str, Any]:
    """构造审计列表查询 body：字符串字段走 .keyword 精确匹配 + ts 倒序 + 分页。"""
    must: list[dict[str, Any]] = []

    def add_term(field: str, val: Any) -> None:
        if val:
            must.append({"term": {f"{field}.keyword": str(val)}})

    add_term("trace_id", filters.get("trace_id"))
    add_term("app_id", filters.get("app_id"))
    add_term("session_id", filters.get("session_id"))
    add_term("request_kind", filters.get("request_kind"))
    add_term("status", filters.get("status"))

    rng: dict[str, Any] = {}
    if filters.get("ts_from"):
        rng["gte"] = str(filters["ts_from"])
    if filters.get("ts_to"):
        rng["lte"] = str(filters["ts_to"])
    if rng:
        must.append({"range": {"ts": rng}})

    query: dict[str, Any]
    if must:
        query = {"bool": {"must": must}}
    else:
        query = {"match_all": {}}

    size = max(1, min(int(filters.get("size") or 50), 200))
    offset = max(0, int(filters.get("offset") or 0))
    return {
        "size": size,
        "from": offset,
        "query": query,
        "sort": [{"ts": {"order": "desc"}}],
    }


def slim_audit_row(src: dict[str, Any]) -> dict[str, Any]:
    """审计列表精简行（详情另调 /api/audit/requests/{trace_id}）。"""
    result = src.get("result") or {}
    return {
        "trace_id": src.get("trace_id"),
        "session_id": src.get("session_id"),
        "app_id": src.get("app_id"),
        "request_kind": src.get("request_kind"),
        "ts": src.get("ts"),
        "elapsed_ms": src.get("elapsed_ms"),
        "status": src.get("status"),
        "input_sku_id": (src.get("input") or {}).get("input_sku_id"),
        "outfit_id": (src.get("input") or {}).get("outfit_id"),
        "outfit_count": len(result.get("outfits") or []),
    }


class RequestAuditLogger:
    """对外请求审计 ES 写/查；不可用或关闭时静默降级。"""

    def __init__(
        self,
        es: Any = None,
        enabled: Optional[bool] = None,
    ) -> None:
        if es is not None:
            self._es = es
        else:
            from backend.retrieval.es_client import EsClient
            self._es = EsClient()
        self._enabled = (
            get_request_audit_enabled() if enabled is None else bool(enabled)
        )

    @property
    def enabled(self) -> bool:
        return bool(self._enabled) and bool(
            getattr(self._es, "available", False),
        )

    def write(self, doc: dict[str, Any]) -> None:
        """写一条审计文档；关闭/不可用/失败均静默。"""
        if not self._enabled:
            return
        try:
            self._es.index_doc("requests", doc, refresh=False)
        except Exception:  # noqa: BLE001
            logger.warning("request audit write failed", exc_info=True)

    def search(self, body: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        if not self.enabled:
            return []
        try:
            return self._es.search_docs("requests", body)
        except Exception:  # noqa: BLE001
            logger.warning("request audit search failed", exc_info=True)
            return []

    def get_by_trace_id(self, trace_id: str) -> Optional[dict[str, Any]]:
        if not self.enabled or not trace_id:
            return None
        rows = self.search({
            "size": 1,
            "query": {"term": {"trace_id.keyword": str(trace_id)}},
        })
        return rows[0][1] if rows else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_request_audit.py -v`
Expected: PASS（全部用例）。

- [ ] **Step 5: Commit**

```bash
git add backend/services/request_audit.py tests/test_request_audit.py
git commit -m "feat(audit): RequestAuditLogger + build_*_doc 纯函数"
```

---

### Task 4: recommend 路径接入审计（采集事件 + 写文档）

**Files:**
- Modify: `backend/services/recommend_service.py`（`__init__`、`external_recommend`、新增 `_write_recommend_audit`、import）
- Modify: `backend/main.py`（`v1_outfit_recommend` 传入 trace_id/app_id/caller）
- Test: `tests/test_request_audit.py`（追加 recommend 接入用例）

**Interfaces:**
- Consumes: Task 3 的 `RequestAuditLogger`、`build_input_block`、`build_recommend_doc`、`now_iso`。
- Produces: `external_recommend(self, req, *, trace_id=None, app_id=None, caller=None)`；`_write_recommend_audit(self, req, image_base64, session_id, captured, status, error, trace_id, app_id, caller, t0, outfits_out)`。

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_request_audit.py`：

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_request_audit.py::ExternalRecommendAuditTest -v`
Expected: FAIL — `external_recommend` 无关键字参数 / 不写审计。

- [ ] **Step 3: Update imports in recommend_service.py**

在 `backend/services/recommend_service.py` 顶部 import 区（已有 `from backend.jsonl_logger import JsonlLogger` 附近）加：

```python
from backend.services.request_audit import (
    RequestAuditLogger,
    build_input_block,
    build_recommend_doc,
    build_regenerate_doc,
    now_iso,
)
```

- [ ] **Step 4: Instantiate audit logger in `__init__`**

在 `RecommendService.__init__` 的 `self._log = JsonlLogger()` 之后加：

```python
        self._audit = RequestAuditLogger()
```

- [ ] **Step 5: Rewrite `external_recommend` to capture events and write audit**

替换 `external_recommend` 整个方法（保持抓图/ChatRequest/reshape 逻辑不变，仅加采集与审计）：

```python
    async def external_recommend(
        self,
        req: ExternalRecommendRequest,
        *,
        trace_id: str | None = None,
        app_id: str | None = None,
        caller: str | None = None,
    ) -> dict[str, Any]:
        """对外搭配推荐：复用 chat_stream 引擎，reshape 为文档出参 + 审计落库。"""
        t0 = perf_counter()
        session_id = (req.session_id or "").strip() or uuid.uuid4().hex
        # 入参 strip 一次：ES 查询与响应回显统一使用 strip 后的 sku_id，
        # 避免调用方误以为返回的 sku 与入参完全一致（SS-02）。
        input_sku_id = (req.input_sku_id or "").strip()
        image_base64: str | None = None
        if req.image_url:
            image_base64 = fetch_image_url_to_base64(req.image_url)
            if image_base64 is None:
                logger.warning(
                    "[对外接口] image_url 抓取失败，降级为仅用 input_sku_id 锚点: %s",
                    (req.image_url or "")[:120],
                )

        chat_req = ChatRequest(
            session_id=session_id,
            message=req.message or "",
            image_base64=image_base64,
            selected_sku_id=input_sku_id or None,
            enable_tryon=bool(req.tryon),
            skip_reason=False,  # 文档要求每套返回 reason
        )

        outfits: list[dict[str, Any]] = []
        captured: dict[str, Any] = {"recall_progress": []}
        status = "ok"
        error: str | None = None
        outfits_out: list[dict[str, Any]] = []
        try:
            async for ev in self.chat_stream(chat_req):
                et = str(ev.get("type") or "")
                if et == "outfit_results":
                    outfits = list(ev.get("outfits") or [])
                elif et == "intent":
                    captured["intent"] = ev
                elif et == "anchor_skus":
                    captured["anchor_skus"] = ev
                elif et == "recall_done":
                    captured["recall_done"] = ev
                elif et == "ranking_reason_done":
                    captured["ranking_reason_done"] = ev
                elif et == "recall_progress":
                    captured["recall_progress"].append(ev)
            out = reshape_outfits_to_external(
                outfits,
                input_sku_id=input_sku_id,
                image_url=req.image_url,
                session_id=session_id,
                data_facade=self._data,
            )
            outfits_out = list(out.get("outfits") or [])
            return out
        except Exception as exc:
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._write_recommend_audit(
                req, image_base64, session_id, captured, status, error,
                trace_id, app_id, caller, t0, outfits_out,
            )

    def _write_recommend_audit(
        self,
        req: ExternalRecommendRequest,
        image_base64: str | None,
        session_id: str,
        captured: dict[str, Any],
        status: str,
        error: str | None,
        trace_id: str | None,
        app_id: str | None,
        caller: str | None,
        t0: float,
        outfits_out: list[dict[str, Any]],
    ) -> None:
        """拼 recommend 审计文档并写 ES；关闭/失败均静默。"""
        if not self._audit.enabled:
            return
        try:
            input_block = build_input_block(
                input_sku_id=(req.input_sku_id or "").strip(),
                image_url=req.image_url,
                image_base64=image_base64,
                message=req.message,
                tryon=req.tryon,
                reason_style=req.reason_style,
            )
            captured["outfits"] = outfits_out
            meta = {
                "trace_id": trace_id,
                "session_id": session_id,
                "app_id": app_id,
                "caller": caller,
                "ts": now_iso(),
                "elapsed_ms": int((perf_counter() - t0) * 1000),
                "status": status,
                "error": error,
            }
            doc = build_recommend_doc(
                input_block=input_block, captured=captured, meta=meta,
            )
            self._audit.write(doc)
        except Exception:  # noqa: BLE001
            logger.warning("request audit (recommend) failed", exc_info=True)
```

- [ ] **Step 6: Pass trace_id/app_id/caller from the endpoint**

在 `backend/main.py` 的 `v1_outfit_recommend` 中，把当前 `out = await _svc.external_recommend(body)` 一行替换为：

```python
    caller_info = getattr(request.state, "caller", None)
    caller_app_id = (
        caller_info.get("app_id")
        if isinstance(caller_info, dict) else None
    )
    out = await _svc.external_recommend(
        body,
        trace_id=_request_trace_id(request),
        app_id=app_id,
        caller=caller_app_id,
    )
```

（`app_id` 已在该函数前面由 `body.app_id` 解析；保持不变。）

- [ ] **Step 7: Run test to verify it passes**

Run: `python3 -m pytest tests/test_request_audit.py::ExternalRecommendAuditTest -v`
Expected: PASS（3 用例）。

- [ ] **Step 8: Commit**

```bash
git add backend/services/recommend_service.py backend/main.py tests/test_request_audit.py
git commit -m "feat(audit): external_recommend 采集事件并落审计文档"
```

---

### Task 5: regenerate 路径接入审计

**Files:**
- Modify: `backend/services/recommend_service.py`（`external_regenerate`、新增 `_write_regenerate_audit`）
- Modify: `backend/main.py`（`v1_outfit_regenerate` 传入 trace_id/app_id/caller）
- Test: `tests/test_request_audit.py`（追加 regenerate 用例）

**Interfaces:**
- Produces: `external_regenerate(self, req, *, trace_id=None, app_id=None, caller=None)`；`_write_regenerate_audit(self, req, result, status, error, trace_id, app_id, caller, t0)`。
- 行为约束: **保持 `external_regenerate` 原有控制流不变**（成功返回 `{outfit_id,reason}`；未命中返回 `{error:...}` 由端点转 404；内部异常 re-raise → 500）。审计只在 `finally` 追加，不改业务语义、不新增 `HTTPException` 依赖。

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_request_audit.py`：

```python
from backend.models import ExternalRegenerateReasonRequest


class ExternalRegenerateAuditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = _SubRecommendService()

    def test_ok_writes_doc(self) -> None:
        # external_regenerate 命中缓存走 regenerate_outfit_reason；
        # 这里直接校验审计方法在 ok 路径的文档形状。
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_request_audit.py::ExternalRegenerateAuditTest -v`
Expected: FAIL — `_write_regenerate_audit` 不存在。

- [ ] **Step 3: Rewrite `external_regenerate` and add helper**

替换 `backend/services/recommend_service.py` 中 `external_regenerate` 整个方法（把现有逻辑搬进 `try`，外加审计）：

```python
    def external_regenerate(
        self,
        req: ExternalRegenerateReasonRequest,
        *,
        trace_id: str | None = None,
        app_id: str | None = None,
        caller: str | None = None,
    ) -> dict[str, Any]:
        """对外重新生成理由：缓存命中复用现有路径；miss 时 ES 兜底 + 审计落库。

        返回 ``{"outfit_id", "reason"}``（丢弃 item_reasons）。
        reason_style 暂透传不接入。
        """
        t0 = perf_counter()
        status = "ok"
        error: str | None = None
        result: dict[str, Any] | None = None
        try:
            # 1) 优先走缓存（与 /regenerate-reason 同路径）
            cached = self._outfit_cache.get(req.outfit_id)
            if cached:
                r = self.regenerate_outfit_reason(
                    RegenerateReasonRequest(outfit_id=req.outfit_id),
                )
                if "error" not in r:
                    result = {
                        "outfit_id": req.outfit_id,
                        "reason": r.get("reason") or "",
                    }
                    return result
            # 2) 缓存 miss / 失败 → ES 取 outfit 兜底重建 card 再生成理由
            try:
                raw_outfit = self._data.get_outfit(req.outfit_id)
                if not raw_outfit:
                    result = {"error": "outfit not found", "outfit_id": req.outfit_id}
                    status = "error"
                    error = "outfit not found"
                    return result
                card = outfit_card(raw_outfit)
                if not card.get("items"):
                    result = {"error": "outfit has no items", "outfit_id": req.outfit_id}
                    status = "error"
                    error = "outfit has no items"
                    return result
                _key, outfit_reason, _item_reasons = _reason_one_outfit(
                    "", card, _reason_mode(),
                )
                result = {
                    "outfit_id": req.outfit_id,
                    "reason": (outfit_reason or "").strip(),
                }
                return result
            except Exception:
                # 内部故障（ES 不可达 / LLM 超时等）不再伪装成 "outfit not found"，
                # re-raise 交给全局 exception_handler 返回诚实 500 + trace_id。
                logger.warning(
                    "[对外接口·regenerate] outfit_id=%s ES 兜底失败，转为 500",
                    req.outfit_id, exc_info=True,
                )
                status = "error"
                error = "internal error"
                raise
        finally:
            self._write_regenerate_audit(
                req, result, status, error, trace_id, app_id, caller, t0,
            )

    def _write_regenerate_audit(
        self,
        req: ExternalRegenerateReasonRequest,
        result: dict[str, Any] | None,
        status: str,
        error: str | None,
        trace_id: str | None,
        app_id: str | None,
        caller: str | None,
        t0: float,
    ) -> None:
        """拼 regenerate_reason 审计文档并写 ES；关闭/失败均静默。"""
        if not self._audit.enabled:
            return
        try:
            input_block = build_input_block(
                outfit_id=req.outfit_id,
                reason_style=req.reason_style,
            )
            meta = {
                "trace_id": trace_id,
                "session_id": None,
                "app_id": app_id,
                "caller": caller,
                "ts": now_iso(),
                "elapsed_ms": int((perf_counter() - t0) * 1000),
                "status": status,
                "error": error,
            }
            doc = build_regenerate_doc(
                input_block=input_block, result=result, meta=meta,
            )
            self._audit.write(doc)
        except Exception:  # noqa: BLE001
            logger.warning("request audit (regenerate) failed", exc_info=True)
```

> 说明: `outfit_card` / `_reason_one_outfit` / `_reason_mode` / `RegenerateReasonRequest` 在原 `external_regenerate` 中已使用，本就 import 在 `recommend_service.py` 顶部，无需新增 import；本实现不引入 `HTTPException`（404 仍由 `main.py` 端点根据 `{"error":...}` 抛出，与现状一致）。`perf_counter` 已在该文件顶部导入。

- [ ] **Step 4: Pass trace_id/app_id/caller from the endpoint**

在 `backend/main.py` 的 `v1_outfit_regenerate` 中，把 `result = _svc.external_regenerate(body)` 一行替换为：

```python
    caller_info = getattr(request.state, "caller", None)
    caller_app_id = (
        caller_info.get("app_id")
        if isinstance(caller_info, dict) else None
    )
    result = _svc.external_regenerate(
        body,
        trace_id=_request_trace_id(request),
        app_id=caller_app_id,
        caller=caller_app_id,
    )
```

（regenerate 入参无 app_id，用 API Key 绑定的 app_id 作为审计 app_id。）

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_request_audit.py::ExternalRegenerateAuditTest -v`
Expected: PASS（3 用例）。

- [ ] **Step 6: Run full audit suite + smoke**

Run: `python3 -m pytest tests/test_request_audit.py -v`
Expected: PASS（全部）。

- [ ] **Step 7: Commit**

```bash
git add backend/services/recommend_service.py backend/main.py tests/test_request_audit.py
git commit -m "feat(audit): external_regenerate 落审计文档"
```

---

### Task 6: 查询 API — 列表 + 详情

**Files:**
- Modify: `backend/main.py`（新增两个 GET 端点 + import）
- Test: 不新增 main 导入型单测（见下测试策略）；回归 `tests/test_request_audit.py`

**Interfaces:**
- Consumes: Task 3 的 `build_audit_search_body`、`slim_audit_row`；`_svc._audit.search` / `get_by_trace_id`。
- Produces: `GET /api/audit/requests`（返 `{"enabled","items"}`）；`GET /api/audit/requests/{trace_id}`（返完整文档 or 404 or 503）。

> **测试策略**: 这两个端点是 `build_audit_search_body` / `slim_audit_row` / `RequestAuditLogger.search` / `get_by_trace_id`（均已在 Task 3 单测覆盖）之上的薄胶水层。为避免单测 `import backend.main` 触发完整服务构建（`RecommendService` + `EsClient` ping + ANN lifespan），本任务不新增 main 导入型单测，改用「全量回归（确认 main.py 改动无破坏）+ 手动 curl smoke（确认 HTTP 接线）」两层验证。

- [ ] **Step 1: Add imports in main.py**

在 `backend/main.py` 顶部 import 区（与 `from backend.services.recommend_service import RecommendService` 附近）加：

```python
from backend.services.request_audit import (
    build_audit_search_body,
    slim_audit_row,
)
```

- [ ] **Step 2: Add the two endpoints**

在 `backend/main.py` 的 `/v1/outfit/regenerate-reason` 端点之后、`/skus/{sku_id}` 之前插入：

```python
@app.get("/api/audit/requests")
def api_audit_requests(
    trace_id: Optional[str] = None,
    app_id: Optional[str] = None,
    session_id: Optional[str] = None,
    request_kind: Optional[str] = None,
    status: Optional[str] = None,
    ts_from: Optional[str] = None,
    ts_to: Optional[str] = None,
    size: int = 50,
    offset: int = 0,
) -> dict:
    """对外请求审计列表（只读，按 ts 倒序）。审计关闭/ES 不可用时返空。"""
    audit = _svc._audit  # noqa: SLF001
    if not audit.enabled:
        return {"enabled": False, "items": []}
    body = build_audit_search_body({
        "trace_id": trace_id, "app_id": app_id, "session_id": session_id,
        "request_kind": request_kind, "status": status,
        "ts_from": ts_from, "ts_to": ts_to, "size": size, "offset": offset,
    })
    rows = audit.search(body)
    return {"enabled": True, "items": [slim_audit_row(s) for _, s in rows]}


@app.get("/api/audit/requests/{trace_id}")
def api_audit_request_detail(trace_id: str) -> dict:
    """对外请求审计详情（完整文档）。审计不可用 503，未命中 404。"""
    audit = _svc._audit  # noqa: SLF001
    if not audit.enabled:
        raise HTTPException(status_code=503, detail="audit disabled")
    doc = audit.get_by_trace_id((trace_id or "").strip())
    if not doc:
        raise HTTPException(status_code=404, detail="not found")
    return doc
```

- [ ] **Step 3: Run full audit suite to confirm no regression from main.py edits**

Run: `python3 -m pytest tests/test_request_audit.py -v`
Expected: PASS（全部既有用例；本任务不新增用例）。

- [ ] **Step 4: Manual curl smoke (HTTP 接线验证)**

先确认 `backend.main` 可正常导入（无语法/接线错误）:

Run: `python3 -c "import backend.main; print('ok')"`
Expected: 输出 `ok`（不 raise）。

再起服务并打两条请求（审计未启用/ES 不可达时列表返 `enabled:false`，属正常）:

```bash
python3 -m uvicorn backend.main:app --port 8888 &
sleep 3
curl -s "http://localhost:8888/api/audit/requests?size=5" | head -c 300
curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:8888/api/audit/requests/sometid"
```
Expected: 列表返回 JSON（`{"enabled":...,"items":[...]}`）；详情对不存在 trace_id 返回 404 或（审计禁用）503。

- [ ] **Step 5: Commit**

```bash
git add backend/main.py
git commit -m "feat(audit): 新增 /api/audit/requests 列表与详情只读 API"
```

---

### Task 7: 前端审计展示页

**Files:**
- Create: `web/audit.html`
- Create: `web/audit.js`
- Modify: `web/index.html`（header 导航加入口）

**Interfaces:**
- Consumes: `GET /api/audit/requests`（列表）、`GET /api/audit/requests/{trace_id}`（详情）。
- 复用 `/web/styles.css` 主题 token；沿用 `web/index.html` 的 light/dark 主题切换惯例。

- [ ] **Step 1: Create `web/audit.html`**

```html
<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>请求审计 · FILA</title>
    <link rel="stylesheet" href="/web/styles.css?v=20260728-audit" />
    <script>
      (function () {
        var saved = localStorage.getItem('fila_agent_html_theme');
        if (saved === 'light' || saved === 'dark') {
          document.documentElement.dataset.theme = saved;
        }
      })();
    </script>
    <style>
      body { margin: 0; padding: 16px 20px 60px; }
      .audit-header { display: flex; gap: 12px; align-items: center; margin-bottom: 14px; flex-wrap: wrap; }
      .audit-header h1 { font-size: 18px; margin: 0; }
      .audit-filters { display: flex; gap: 8px; flex-wrap: wrap; align-items: end; margin-bottom: 12px; }
      .audit-filters label { display: flex; flex-direction: column; font-size: 12px; gap: 2px; }
      .audit-filters input, .audit-filters select { padding: 4px 6px; font-size: 13px; min-width: 120px; }
      .audit-table { width: 100%; border-collapse: collapse; font-size: 13px; }
      .audit-table th, .audit-table td { border-bottom: 1px solid var(--border, #2a2f3a); padding: 6px 8px; text-align: left; vertical-align: top; }
      .audit-table tr { cursor: pointer; }
      .audit-table tr:hover { background: rgba(127,127,127,0.12); }
      .tag { display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 11px; }
      .tag-ok { background: #1f6f3f; color: #fff; }
      .tag-err { background: #8a2f2f; color: #fff; }
      .tag-recommend { background: #2a4a8a; color: #fff; }
      .tag-regenerate { background: #6a4a2a; color: #fff; }
      #detail { margin-top: 20px; border-top: 2px solid var(--border, #2a2f3a); padding-top: 14px; }
      #detail pre { background: rgba(127,127,127,0.1); padding: 10px; border-radius: 6px; overflow: auto; font-size: 12px; max-height: 60vh; }
      .muted { color: var(--muted, #888); font-size: 12px; }
    </style>
  </head>
  <body>
    <div class="audit-header">
      <h1>请求审计</h1>
      <a href="/web/index.html" class="muted">← 返回调试台</a>
      <span id="audit-state" class="muted"></span>
    </div>

    <div class="audit-filters">
      <label>trace_id<input id="f-trace" /></label>
      <label>app_id<input id="f-app" /></label>
      <label>类型
        <select id="f-kind">
          <option value="">全部</option>
          <option value="recommend">recommend</option>
          <option value="regenerate_reason">regenerate_reason</option>
        </select>
      </label>
      <label>状态
        <select id="f-status">
          <option value="">全部</option>
          <option value="ok">ok</option>
          <option value="error">error</option>
        </select>
      </label>
      <label>起始(>=)<input id="f-from" placeholder="2026-07-28" /></label>
      <label>结束(<=)<input id="f-to" placeholder="2026-07-29" /></label>
      <label>条数<input id="f-size" value="50" /></label>
      <button id="btn-search" type="button">查询</button>
      <button id="btn-reset" type="button">清空</button>
    </div>

    <table class="audit-table">
      <thead>
        <tr>
          <th>ts</th><th>类型</th><th>状态</th><th>trace_id</th>
          <th>app_id</th><th>输入</th><th>套数</th><th>耗时ms</th>
        </tr>
      </thead>
      <tbody id="rows"></tbody>
    </table>

    <div id="detail"></div>

    <script src="/web/audit.js?v=20260728-audit"></script>
  </body>
</html>
```

- [ ] **Step 2: Create `web/audit.js`**

```javascript
"use strict";
var $ = function (id) { return document.getElementById(id); };

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function qs() {
  var p = new URLSearchParams();
  [["f-trace", "trace_id"], ["f-app", "app_id"], ["f-kind", "request_kind"],
   ["f-status", "status"], ["f-from", "ts_from"], ["f-to", "ts_to"],
   ["f-size", "size"]].forEach(function (pair) {
    var v = $(pair[0]).value.trim();
    if (v) p.set(pair[1], v);
  });
  return p.toString();
}

function renderRows(items) {
  var tbody = $("rows");
  tbody.innerHTML = "";
  items.forEach(function (it) {
    var tr = document.createElement("tr");
    var kindTag = '<span class="tag tag-' + (it.request_kind === "recommend" ? "recommend" : "regenerate") + '">' + esc(it.request_kind) + "</span>";
    var statusTag = '<span class="tag ' + (it.status === "ok" ? "tag-ok" : "tag-err") + '">' + esc(it.status) + "</span>";
    var inputTxt = it.outfit_id ? ("outfit=" + esc(it.outfit_id)) : ("sku=" + esc(it.input_sku_id));
    tr.innerHTML =
      "<td>" + esc(it.ts) + "</td>" +
      "<td>" + kindTag + "</td>" +
      "<td>" + statusTag + "</td>" +
      "<td>" + esc(it.trace_id) + "</td>" +
      "<td>" + esc(it.app_id) + "</td>" +
      "<td>" + inputTxt + "</td>" +
      "<td>" + esc(it.outfit_count) + "</td>" +
      "<td>" + esc(it.elapsed_ms) + "</td>";
    tr.addEventListener("click", function () { loadDetail(it.trace_id); });
    tbody.appendChild(tr);
  });
}

function search() {
  $("audit-state").textContent = "加载中…";
  fetch("/api/audit/requests?" + qs())
    .then(function (r) { return r.json(); })
    .then(function (data) {
      $("audit-state").textContent = data.enabled ? ("共 " + data.items.length + " 条") : "审计未启用";
      renderRows(data.items || []);
      $("detail").innerHTML = "";
    })
    .catch(function (e) { $("audit-state").textContent = "查询失败: " + e; });
}

function loadDetail(traceId) {
  $("detail").innerHTML = '<p class="muted">加载 ' + esc(traceId) + " …</p>";
  fetch("/api/audit/requests/" + encodeURIComponent(traceId))
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (doc) {
      $("detail").innerHTML =
        "<h3>详情 " + esc(traceId) + "</h3>" +
        "<pre>" + esc(JSON.stringify(doc, null, 2)) + "</pre>";
    })
    .catch(function (e) {
      $("detail").innerHTML = '<p class="muted">详情加载失败: ' + esc(e.message) + "</p>";
    });
}

window.addEventListener("DOMContentLoaded", function () {
  $("btn-search").addEventListener("click", search);
  $("btn-reset").addEventListener("click", function () {
    ["f-trace", "f-app", "f-kind", "f-status", "f-from", "f-to"].forEach(function (id) { $(id).value = ""; });
    $("f-size").value = "50";
    search();
  });
  search();
});
```

- [ ] **Step 3: Add nav entry in `web/index.html`**

在 `web/index.html` 的 `<div class="header-links">` 块内（已有「商品浏览」链接附近）追加一个链接：

```html
          <a
            href="/web/audit.html"
            target="_blank"
            rel="noopener"
            class="hide-in-presentation"
            >请求审计</a
          >
```

（`hide-in-presentation` 类沿用现有 presentation 模式隐藏惯例；若该类已存在于 `web/styles.css`，直接用；若不存在，去掉该类即可。）

- [ ] **Step 4: Smoke-test the page**

Run: `python3 -m uvicorn backend.main:app --port 8888` 然后浏览器打开 `http://localhost:8888/web/audit.html`：
Expected: 页面加载、显示「审计未启用」或「共 0 条」（取决于 ES 是否可达与索引是否创建）；过滤栏交互正常；`/web/index.html` header 出现「请求审计」链接。

- [ ] **Step 5: Commit**

```bash
git add web/audit.html web/audit.js web/index.html
git commit -m "feat(audit): 新增请求审计只读展示页 web/audit.html"
```

---

## Self-Review Notes

- **Spec coverage**: spec §2 范围（recommend + regenerate）→ Task 4/5；§3 决策（单点采集/一请求一文档/图片只存url+sha1/不阻塞/refresh=False/开关）→ Task 3/4/5 + config；§4 数据模型 → Task 3 `build_*_doc`；§5 文件清单 → 全部任务覆盖；§6 错误处理 → `RequestAuditLogger.write` 吞异常 + 端点 503/404；§7 部署回滚 → Task 1 config 开关 + 索引需预创建（spec 已述）。
- **Refinement vs spec**: spec §4 recall 块原列 `deduped_outfit_ids`，但该 id 列表未被 `chat_stream` 事件携带，强行取需改公共流程，违背「不动 chat_stream」决策；计划改为只落 `recall_done`/`recall_progress` 自带的 count 与 roles（不含 deduped_outfit_ids），并在 §4 文档结构里相应去掉了该字段——属计划期合理细化。
- **Type consistency**: `RequestAuditLogger.write(doc)` / `.search(body)` / `.get_by_trace_id(tid)` 在 Task 3 定义、Task 6 调用，签名一致；`build_recommend_doc`/`build_regenerate_doc`/`build_input_block` 在 Task 3 定义、Task 4/5 调用，参数名一致；`external_recommend`/`external_regenerate` 新增关键字参数在 Task 4/5 定义、main.py 调用一致。
- **Placeholder scan**: 无 TBD/TODO；每个 code step 均含完整可运行代码与命令。
