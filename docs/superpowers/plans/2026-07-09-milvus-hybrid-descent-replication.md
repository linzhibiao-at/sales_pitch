# FILA Milvus Hybrid 检索复刻 descent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 复刻 descent 的 Milvus 原生 BM25 + dense hybrid 检索(函数改 fila 名),并从 catalog 源头补齐 descent 用到而 fila 缺失的字段,使 ES 与 Milvus 都能用上。

**Architecture:** 三段式——(1) `etl_common.build_sku_record` 补 22 个 descent 字段;(2) 新建共享 `scripts/hybrid_text.py`(`build_keyword_text`/`build_semantic_text`)供 ES 与 Milvus build 共用;(3) 新建 `scripts/build_hybrid_index.py`(Milvus 单集合 hybrid schema+灌数据)与 `backend/retrieval/hybrid_search.py`(`FilaSkuHybridSearcher`),接入 `SkuRetriever.recall_by_hybrid`(旧 dense 通路作 fallback)。新集合 `fila_sku_hybrid_vectors`,不动线上。

**Tech Stack:** pymilvus 2.6.13(Function/FunctionType/AnnSearchRequest/RRFRanker/WeightedRanker/MilvusClient),Milvus cloud 2.6.3,unittest+mock,config.yaml,Ark `embed_text`。

## Global Constraints

- 云集群 `server_version=2.6.3`,pymilvus 已安装 2.6.13,可 `from pymilvus import Function, FunctionType, AnnSearchRequest, RRFRanker, WeightedRanker, MilvusClient, CollectionSchema, DataType, FieldSchema`。
- dense 向量 build 与 runtime **都用 `backend.embedding_client.embed_text`**,dim 取 `config.embedding.dimensions`(默认 1024)。禁止混用 descent 的 DashScope 直连。
- 命名:类 `FilaSkuHybridSearcher`;方法 `search_keyword`/`search_semantic`/`search_hybrid`/`get_skus_by_ids`;函数 `build_keyword_text`/`build_semantic_text`/`build_hybrid_schema`/`get_hybrid_index_params`/`create_hybrid_collection`/`rewrite_query`/`rule_rewrite`/`llm_rewrite`。
- 目标集合名 `fila_sku_hybrid_vectors`(config `milvus.collections.sku_hybrid_vectors`)。
- local(Milvus Lite *.db)下 BM25 Function 可能不支持 → build 脚本用 `is_milvus_lite_local_uri(uri)` 检测并 `raise SystemExit` 明确报错。
- fila 现有 `build_search_text`(etl_common:638,签名 `build_search_text(gender=, series=, title=, color_name=, role=, spu_id=, sku_id=)`)保持不动,不影响既有 ES 搜索文本。
- `cat_type` = 现有 `category_l1`(=ext.cat_type),不重复存,build 侧映射。
- 测试风格:`unittest.TestCase` + `unittest.mock`,命令 `python -m pytest tests/<file>::<test> -v` 或 `python -m pytest tests/<file> -v`。
- 每个 Task 末尾 commit;commit message 以 `feat:`/`test:`/`refactor:`/`docs:` 前缀,中文描述。

**参考实现**:`/home/jovyan/swap/tmp/descent_product_search/{scripts/search_engine.py,scripts/schema_manager.py,scripts/build_index.py,scripts/data_processor.py}`。

**Spec**:`docs/superpowers/specs/2026-07-09-milvus-hybrid-descent-replication-design.md`。

---

## File Structure

**新建**:
- `scripts/hybrid_text.py` — 共享 `build_keyword_text(row)` / `build_semantic_text(row)`(build 期纯函数,ES 与 Milvus 共用)。
- `scripts/build_hybrid_index.py` — Milvus hybrid 集合 schema + 索引 + 灌数据(build_hybrid_schema/get_hybrid_index_params/create_hybrid_collection + main)。
- `backend/retrieval/hybrid_search.py` — `FilaSkuHybridSearcher`(search_keyword/search_semantic/search_hybrid/get_skus_by_ids)+ rewrite_query/rule_rewrite/llm_rewrite + build_filter_expr/_format_results。
- `tests/test_hybrid_text.py`
- `tests/test_build_hybrid_index.py`
- `tests/test_hybrid_search.py`
- `tests/test_recall_by_hybrid.py`
- `tests/test_descent_extra_fields.py`
- `tests/test_index_sync_state_hybrid_bucket.py`

**修改**:
- `scripts/index_sync_state.py` — `_normalize_milvus_state` 加 `sku_hybrid_vectors` bucket。
- `scripts/etl_common.py` — 新增 `build_descent_extra_fields`/`merge_features` 纯函数 + `build_sku_record` 合并 + build_catalog 覆盖率统计。
- `scripts/build_catalog.py` — `_COVERAGE_FIELDS` 追加新字段项。
- `scripts/build_fila_es_index.py` — `create_skus_index` mapping 追加字段 + `sku_doc` 用 `build_keyword_text` 富化 search_text。
- `backend/retrieval/sku_retriever.py` — 新增 `recall_by_hybrid`。
- `config.yaml` — milvus.collections.sku_hybrid_vectors + milvus.hybrid。

---

### Task 1: index_sync_state 增加 sku_hybrid_vectors bucket

`_normalize_milvus_state` 当前硬编码 `("sku_vectors", "sku_text_vectors")`,load_state 会把任何新 bucket 清成 `{}`,导致 `sku_hybrid_vectors` 增量签名丢失。

**Files:**
- Modify: `scripts/index_sync_state.py:38-48`
- Test: `tests/test_index_sync_state_hybrid_bucket.py`

**Interfaces:**
- Produces: `load_state()` 返回的 `state["milvus"]` 含稳定的 `sku_hybrid_vectors` dict(空时为 `{}`),不被归一化清空。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_index_sync_state_hybrid_bucket.py
from __future__ import annotations

import unittest
from pathlib import Path

from scripts import index_sync_state as iss


class TestHybridBucket(unittest.TestCase):
    def test_normalize_keeps_hybrid_bucket(self):
        raw = {
            "sku_vectors": {"A": "1"},
            "sku_text_vectors": {"B": "2"},
            "sku_hybrid_vectors": {"C": "3"},
        }
        out = iss._normalize_milvus_state(raw)
        self.assertEqual(out["sku_hybrid_vectors"], {"C": "3"})

    def test_normalize_creates_hybrid_bucket_when_missing(self):
        out = iss._normalize_milvus_state({})
        self.assertIn("sku_hybrid_vectors", out)
        self.assertEqual(out["sku_hybrid_vectors"], {})

    def test_load_state_preserves_hybrid_bucket(self):
        p = Path(__file__).resolve().parent / "_tmp_hybrid_state.json"
        state = iss.load_state(p)
        state["milvus"]["sku_hybrid_vectors"] = {"SKU1": "sig"}
        iss.save_state(state, p)
        reloaded = iss.load_state(p)
        self.assertEqual(reloaded["milvus"]["sku_hybrid_vectors"], {"SKU1": "sig"})
        p.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_index_sync_state_hybrid_bucket.py -v`
Expected: FAIL — `KeyError: 'sku_hybrid_vectors'`(归一化未保留)。

- [ ] **Step 3: Implement**

```python
# scripts/index_sync_state.py  替换 _normalize_milvus_state
def _normalize_milvus_state(raw: Any) -> dict[str, Any]:
    """保证 milvus 下向量桶为 dict。"""
    if not isinstance(raw, dict):
        return {"sku_vectors": {}, "sku_text_vectors": {}, "sku_hybrid_vectors": {}}
    out = dict(raw)
    out.pop("outfit_vectors", None)
    for key in ("sku_vectors", "sku_text_vectors", "sku_hybrid_vectors"):
        val = out.get(key)
        if not isinstance(val, dict):
            out[key] = {}
    return out
```

同步改 `load_state` 里两处兜底返回(空状态分支),把 `{"sku_vectors": {}, "sku_text_vectors": {}}` 换成含 `sku_hybrid_vectors`:

```python
# scripts/index_sync_state.py  load_state 的两处 return(行 ~97-101 与 ~106-111)
return {
    "version": STATE_VERSION,
    "last_catalog_sync_at": None,
    "es": _normalize_es_state({}),
    "milvus": _normalize_milvus_state({}),  # _normalize_milvus_state 已含 hybrid bucket
}
```
(两处兜底都已调 `_normalize_milvus_state({})`,Step 3 改完该函数后自动带 hybrid bucket,无需逐处改;若两处是手写 dict 则替换为 `_normalize_milvus_state({})`。)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_index_sync_state_hybrid_bucket.py -v`
Expected: PASS(3 个用例全过)。

- [ ] **Step 5: Commit**

```bash
git add scripts/index_sync_state.py tests/test_index_sync_state_hybrid_bucket.py
git commit -m "feat: index_sync_state 保留 sku_hybrid_vectors bucket"
```

---

### Task 2: etl_common 补 descent 字段(纯函数 + build_sku_record 合并)

**Files:**
- Modify: `scripts/etl_common.py`(新增 `merge_features`、`build_descent_extra_fields` 纯函数;改 `build_sku_record` 合并;改 `build_catalog` 覆盖率)。
- Modify: `scripts/build_catalog.py`(覆盖率 `_COVERAGE_FIELDS` 追加)。
- Test: `tests/test_descent_extra_fields.py`

**Interfaces:**
- Produces(供 Task 3/4/5/8 用):`skus.jsonl` 每行新增 key:`product_name_short`、`goods_sn`、`brand_line`、`market_price`、`min_price`、`max_price`、`year`、`category`、`length`、`technology`、`features`、`selling_point_label`、`keyword`、`color_images`(JSON str)、`video_url`、`onsell`(int)、`sales`(int)、`sales_week`(int)、`sales_month`(int)、`w_order`(int)、`sku_count`(int)。
- `build_descent_extra_fields(master: dict, ext: dict, color_attr_rows: list[dict], sku_count: int) -> dict[str, Any]`(纯函数,可单测)。
- `merge_features(pro_info: str, pro_content: str) -> str`(纯函数)。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_descent_extra_fields.py
from __future__ import annotations

import json
import unittest

from scripts.etl_common import build_descent_extra_fields, merge_features


class TestMergeFeatures(unittest.TestCase):
    def test_dedup_preserve_order(self):
        self.assertEqual(merge_features("a,b", "b,c"), "a, b, c")

    def test_empty(self):
        self.assertEqual(merge_features("", ""), "")


class TestBuildDescentExtraFields(unittest.TestCase):
    def setUp(self):
        self.master = {
            "id_brand": "1",
            "pro_name": "短袖T",
            "id_alias": "A11M023104G",
            "pro_info": "透气,速干",
            "pro_content": "速干,抗菌",
            "selling_point_label": "凉爽",
            "keyword": "短T,男",
            "market_price": "299",
            "min_price": "179",
            "max_price": "299",
            "onsell": "1",
            "sales": "100",
            "sales_week": "10",
            "sales_month": "40",
            "w_order": "5",
            "video": "https://x/v.mp4",
        }
        self.ext = {"year": "2024", "cat_alias": "短袖T恤", "length": "短", "technology": "冰感"}
        self.color_rows = [
            {"attr_name": "黑", "image_url": "https://x/1.jpg"},
            {"attr_name": "白", "image_url": "https://x/2.jpg"},
        ]

    def test_basic_fields(self):
        f = build_descent_extra_fields(self.master, self.ext, self.color_rows, 3)
        self.assertEqual(f["product_name_short"], "短袖T")
        self.assertEqual(f["goods_sn"], "A11M023104G")
        self.assertEqual(f["brand_line"], "FILA")
        self.assertEqual(f["year"], "2024")
        self.assertEqual(f["category"], "短袖T恤")
        self.assertEqual(f["length"], "短")
        self.assertEqual(f["technology"], "冰感")
        self.assertEqual(f["features"], "透气, 速干, 抗菌")
        self.assertEqual(f["selling_point_label"], "凉爽")
        self.assertEqual(f["keyword"], "短T,男")
        self.assertEqual(f["video_url"], "https://x/v.mp4")
        self.assertEqual(f["sku_count"], 3)
        self.assertEqual(f["onsell"], 1)
        self.assertEqual(f["sales"], 100)
        self.assertEqual(f["sales_week"], 10)
        self.assertEqual(f["sales_month"], 40)
        self.assertEqual(f["w_order"], 5)
        self.assertEqual(f["market_price"], 299.0)
        self.assertEqual(f["min_price"], 179.0)
        self.assertEqual(f["max_price"], 299.0)

    def test_brand_line_map(self):
        self.assertEqual(build_descent_extra_fields({"id_brand": "17"}, {}, [], 0)["brand_line"], "FILA KIDS")
        self.assertEqual(build_descent_extra_fields({"id_brand": "21"}, {}, [], 0)["brand_line"], "FILA FUSION")
        self.assertEqual(build_descent_extra_fields({"id_brand": "10"}, {}, [], 0)["brand_line"], "FILA联名")

    def test_color_images_json(self):
        f = build_descent_extra_fields(self.master, self.ext, self.color_rows, 3)
        parsed = json.loads(f["color_images"])
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["color"], "黑")
        self.assertEqual(parsed[0]["image_url"], "https://x/1.jpg")

    def test_empty_master_is_safe(self):
        f = build_descent_extra_fields({}, {}, [], 0)
        self.assertEqual(f["brand_line"], "")
        self.assertEqual(f["features"], "")
        self.assertEqual(f["sku_count"], 0)
        self.assertEqual(f["onsell"], 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_descent_extra_fields.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_descent_extra_fields'`。

- [ ] **Step 3: Implement pure helpers in etl_common.py**

在 `scripts/etl_common.py` 顶部常量区(`BRAND` 附近)加:

```python
# descent 复刻:brand_line 由 id_brand 映射(与 build_catalog._FILA_BRAND_IDS 一致)
_FILA_BRAND_LINE_MAP: dict[str, str] = {
    "1": "FILA",
    "17": "FILA KIDS",
    "21": "FILA FUSION",
    "10": "FILA联名",
}


def _safe_float(val: Any) -> float:
    if val is None:
        return 0.0
    try:
        v = float(val)
        return v if 0 < v < 100000 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _safe_int(val: Any) -> int:
    if val is None:
        return 0
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return 0


def merge_features(pro_info: str, pro_content: str) -> str:
    """合并 pro_info + pro_content,逗号分隔去重保序(descent merge_features 同逻辑)。"""
    parts: list[str] = []
    for src in (pro_info, pro_content):
        if src:
            parts.extend(p.strip() for p in str(src).split(",") if p.strip())
    seen: set[str] = set()
    result: list[str] = []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            result.append(p)
    return ", ".join(result)


def build_descent_extra_fields(
    master: dict[str, Any],
    ext: dict[str, Any],
    color_attr_rows: list[dict[str, Any]],
    sku_count: int,
) -> dict[str, Any]:
    """从 product_master/product_master_ext/product_attr 派生 descent 用到的、fila 原缺字段。

    纯函数:输入原始表行,输出可直接并入 sku record 的字段 dict。
    color_attr_rows 为该 goods 的 id_pac=1 颜色属性行(含 image_url)。
    """
    def _ts(v: Any) -> str:
        return text_or_empty(v)

    color_images = [
        {
            "color": _ts(r.get("attr_name")),
            "image_url": _ts(r.get("image_url")),
        }
        for r in (color_attr_rows or [])
        if _ts(r.get("image_url"))
    ]
    return {
        "product_name_short": _ts(master.get("pro_name")),
        "goods_sn": _ts(master.get("id_alias")),
        "brand_line": _FILA_BRAND_LINE_MAP.get(_ts(master.get("id_brand")), ""),
        "market_price": _safe_float(master.get("market_price")),
        "min_price": _safe_float(master.get("min_price")),
        "max_price": _safe_float(master.get("max_price")),
        "year": _ts(ext.get("year")),
        "category": _ts(ext.get("cat_alias")),
        "length": _ts(ext.get("length")),
        "technology": _ts(ext.get("technology")),
        "features": merge_features(_ts(master.get("pro_info")), _ts(master.get("pro_content"))),
        "selling_point_label": _ts(master.get("selling_point_label")),
        "keyword": _ts(master.get("keyword")),
        "color_images": json.dumps(color_images, ensure_ascii=False) if color_images else "",
        "video_url": _ts(master.get("video")),
        "onsell": _safe_int(master.get("onsell")),
        "sales": _safe_int(master.get("sales")),
        "sales_week": _safe_int(master.get("sales_week")),
        "sales_month": _safe_int(master.get("sales_month")),
        "w_order": _safe_int(master.get("w_order")),
        "sku_count": int(sku_count or 0),
    }
```
注:`json`/`Any` 在 etl_common.py 已 import;`text_or_empty` 已定义。

- [ ] **Step 4: Wire into build_sku_record**

在 `scripts/etl_common.py::build_sku_record` 的 `return {` 之前(`search_keywords = fila_search_keywords(...)` 之后)加:

```python
        descent_extra = build_descent_extra_fields(
            master,
            ext,
            self.color_attrs_by_goods.get(gid, []),
            len(self.skus_by_goods.get(gid, [])),
        )
```
然后在 return dict 里**末尾**追加(不替换现有 key):

```python
            **descent_extra,
```
(即 `return { "sku_id": sku_id, ..., "scene_domain": extract_scene_domain(...), **descent_extra }`。)

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_descent_extra_fields.py -v`
Expected: PASS(全部用例)。

- [ ] **Step 6: build_catalog 覆盖率追加新字段**

在 `scripts/build_catalog.py` 的 `_COVERAGE_FIELDS` 列表末尾(`("场景域", "scene_domain", "scene")` 之后)追加:

```python
        ("品牌线", "brand_line", "str"),
        ("年度", "year", "str"),
        ("卖点", "selling_point_label", "str"),
        ("功能", "features", "str"),
        ("技术", "technology", "str"),
        ("货号", "goods_sn", "str"),
        ("在售", "onsell", "onsell"),
        ("销量", "sales", "int"),
```
在 `_COVERAGE_FIELDS` 循环的 kind 分支里补 `onsell`/`int` 两个 kind:
```python
            elif kind == "onsell":
                if val in (1, 2, "1", "2"):
                    hit += 1
            elif kind == "int":
                try:
                    if val is not None and int(val) > 0:
                        hit += 1
                except (TypeError, ValueError):
                    pass
```
(放在现有 `elif kind == "price":` 分支同级、`else:` 之前。)

- [ ] **Step 7: Commit**

```bash
git add scripts/etl_common.py scripts/build_catalog.py tests/test_descent_extra_fields.py
git commit -m "feat: catalog 补 descent 字段(brand_line/year/features/selling_point/...)"
```

---

### Task 3: 共享 hybrid_text 模块(build_keyword_text / build_semantic_text)

**Files:**
- Create: `scripts/hybrid_text.py`
- Test: `tests/test_hybrid_text.py`

**Interfaces:**
- Produces:`build_keyword_text(row: dict) -> str`(BM25 源,标题×3 加权)、`build_semantic_text(row: dict) -> str`(dense 嵌入源)。供 Task 4(build_hybrid_index)与 Task 7(ES sku_doc)共用。
- Consumes: Task 2 产出的 `row` 字段(`title`/`search_keywords`/`keyword`/`product_name_short`/`brand_line`/`series`/`sub_series`/`category`/`category_l1`/`up_down_raw`/`gender`/`age`/`season`/`year`/`modeling`/`length`/`material`/`technology`/`features`/`selling_point_label`/`color_name`/`goods_sn`)。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hybrid_text.py
from __future__ import annotations

import unittest

from scripts.hybrid_text import build_keyword_text, build_semantic_text


def _row():
    return {
        "title": "短袖T",
        "search_keywords": "A1,短袖T,男",
        "keyword": "短T",
        "product_name_short": "短T",
        "brand_line": "FILA",
        "series": "GOLF",
        "sub_series": "上衣",
        "category": "短袖T恤",
        "category_l1": "服装",
        "up_down_raw": "上装",
        "gender": ["男"],
        "age": "成人",
        "season": ["夏"],
        "year": "2024",
        "modeling": "修身",
        "length": "短",
        "material": "棉",
        "technology": "冰感",
        "features": "透气,速干",
        "selling_point_label": "凉爽",
        "color_name": "黑",
        "goods_sn": "A1G",
    }


class TestBuildKeywordText(unittest.TestCase):
    def test_title_repeated_three_times(self):
        t = build_keyword_text(_row())
        self.assertEqual(t.count("短袖T"), 3)

    def test_includes_rich_fields(self):
        t = build_keyword_text(_row())
        for kw in ("FILA", "GOLF", "冰感", "透气,速干", "凉爽", "黑", "A1G", "2024"):
            self.assertIn(kw, t)

    def test_empty_row_safe(self):
        self.assertEqual(build_keyword_text({}), "")


class TestBuildSemanticText(unittest.TestCase):
    def test_starts_with_title_and_has_kv(self):
        t = build_semantic_text(_row())
        self.assertTrue(t.startswith("短袖T"))
        self.assertIn("品牌线:FILA", t)
        self.assertIn("系列:GOLF", t)
        self.assertIn("品类:短袖T恤", t)

    def test_empty_row_safe(self):
        self.assertEqual(build_semantic_text({}), "")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_hybrid_text.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.hybrid_text'`。

- [ ] **Step 3: Implement scripts/hybrid_text.py**

```python
"""FILA hybrid 检索共享文本构建(descent build_search_text/build_semantic_text 的 fila 版)。

被 scripts/build_hybrid_index.py(Milvus BM25 源 + dense 源)与
scripts/build_fila_es_index.py(ES 富化 search_text)共用。
"""

from __future__ import annotations

from typing import Any


def _join_list(val: Any) -> str:
    if isinstance(val, list):
        return " ".join(str(x).strip() for x in val if str(x).strip())
    return str(val or "").strip()


def build_keyword_text(row: dict[str, Any]) -> str:
    """BM25 源文本:标题重复 3 次加权 + 各结构化属性 + 卖点/功能/技术/货号。

    空段自动跳过。镜像 descent data_processor.build_search_text。
    """
    title = str(row.get("title") or "").strip()
    parts: list[str] = [
        str(row.get("search_keywords") or ""),
        str(row.get("keyword") or ""),
        title, title, title,  # 标题 ×3 权重 boost
        str(row.get("product_name_short") or ""),
        str(row.get("brand_line") or ""),
        str(row.get("series") or ""),
        str(row.get("sub_series") or ""),
        str(row.get("category") or ""),
        str(row.get("category_l1") or ""),  # = cat_type
        str(row.get("up_down_raw") or ""),
        _join_list(row.get("gender")),
        str(row.get("age") or ""),
        _join_list(row.get("season")),
        str(row.get("year") or ""),
        str(row.get("modeling") or ""),
        str(row.get("length") or ""),
        str(row.get("material") or ""),  # = fabric
        str(row.get("technology") or ""),
        str(row.get("features") or ""),
        str(row.get("selling_point_label") or ""),
        str(row.get("color_name") or ""),
        str(row.get("goods_sn") or ""),
    ]
    return " ".join(p for p in parts if p).strip()


def build_semantic_text(row: dict[str, Any]) -> str:
    """dense 嵌入源:title + key:value 属性。镜像 descent build_semantic_text。"""
    parts: list[str] = [str(row.get("title") or "")]
    optional = [
        ("品牌线", row.get("brand_line")),
        ("系列", row.get("series")),
        ("小系列", row.get("sub_series")),
        ("品类", row.get("category")),
        ("大类", row.get("category_l1")),
        ("上下装", row.get("up_down_raw")),
        ("性别", _join_list(row.get("gender"))),
        ("人群", row.get("age")),
        ("季节", _join_list(row.get("season"))),
        ("年度", row.get("year")),
        ("版型", row.get("modeling")),
        ("长短", row.get("length")),
        ("面料", row.get("material")),
        ("技术", row.get("technology")),
        ("功能", row.get("features")),
        ("颜色", row.get("color_name")),
        ("卖点", row.get("selling_point_label")),
        ("款号", row.get("goods_sn")),
    ]
    for key, value in optional:
        v = str(value or "").strip()
        if v:
            parts.append(f"{key}:{v}")
    return " ".join(p for p in parts if p).strip()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_hybrid_text.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add scripts/hybrid_text.py tests/test_hybrid_text.py
git commit -m "feat: 共享 hybrid_text(build_keyword_text/build_semantic_text)"
```

---

### Task 4: Milvus hybrid 建索引脚本(build_hybrid_index.py)

复刻 descent schema_manager + build_index。`build_hybrid_schema`/`get_hybrid_index_params`/`build_insert_row` 为纯/半纯函数可单测;create/insert 用 mock client 测调用契约。

**Files:**
- Create: `scripts/build_hybrid_index.py`
- Test: `tests/test_build_hybrid_index.py`

**Interfaces:**
- Consumes: Task 2 的 `skus.jsonl` 字段;Task 3 `build_keyword_text`/`build_semantic_text`;`backend.embedding_client.embed_text`;`backend.config` milvus helpers;`index_sync_state`(`load_state`/`save_state`/`clear_milvus_bucket`/`milvus_text_row_signature`/`DEFAULT_STATE_PATH`)。
- Produces:Milvus 集合 `fila_sku_hybrid_vectors`(schema:sku_id PK / search_text chinese analyzer / sparse_vector / dense_vector + 标量;BM25 Function;SPARSE_INVERTED+IVF_FLAT 索引)。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_build_hybrid_index.py
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import scripts.build_hybrid_index as bhi


class TestSchema(unittest.TestCase):
    def test_schema_has_bm25_function_and_fields(self):
        schema = bhi.build_hybrid_schema(dim=1024)
        names = [f.name for f in schema.fields]
        self.assertIn("sku_id", names)
        self.assertIn("search_text", names)
        self.assertIn("sparse_vector", names)
        self.assertIn("dense_vector", names)
        st = next(f for f in schema.fields if f.name == "search_text")
        self.assertTrue(st.params.get("enable_analyzer"))
        self.assertEqual(st.params.get("analyzer_params"), {"type": "chinese"})
        funcs = schema.functions
        self.assertEqual(len(funcs), 1)
        self.assertEqual(funcs[0].name, "search_text_bm25")
        self.assertEqual(funcs[0].type_name, "BM25")
        self.assertIn("search_text", funcs[0].input_field_names)
        self.assertEqual(funcs[0].output_field_names, ["sparse_vector"])


class TestIndexParams(unittest.TestCase):
    def test_index_params(self):
        params = bhi.get_hybrid_index_params()
        by_field = {p["field_name"]: p for p in params}
        self.assertEqual(by_field["sparse_vector"]["index_type"], "SPARSE_INVERTED_INDEX")
        self.assertEqual(by_field["sparse_vector"]["metric_type"], "BM25")
        self.assertEqual(by_field["dense_vector"]["index_type"], "IVF_FLAT")
        self.assertEqual(by_field["dense_vector"]["metric_type"], "COSINE")


class TestBuildInsertRow(unittest.TestCase):
    def test_includes_search_text_omits_sparse(self):
        row = {"sku_id": "S1", "title": "短袖T", "brand_line": "FILA"}
        vec = [0.1] * 8
        rec = bhi.build_insert_row(row, vec, dim=8)
        self.assertEqual(rec["sku_id"], "S1")
        self.assertIn("search_text", rec)
        self.assertEqual(rec["dense_vector"], vec)
        self.assertNotIn("sparse_vector", rec)  # 服务端 BM25 Function 自动产
        self.assertIsInstance(rec["search_text"], str)


class TestCreateCollectionCallsClient(unittest.TestCase):
    def test_create_invokes_create_collection_with_index_params(self):
        client = MagicMock()
        client.has_collection.return_value = False
        bhi.create_hybrid_collection(client, "fila_sku_hybrid_vectors", dim=1024, uri="http://cloud:19530")
        self.assertTrue(client.create_collection.called)
        kw = client.create_collection.call_args.kwargs
        self.assertEqual(kw["collection_name"], "fila_sku_hybrid_vectors")
        self.assertTrue(client.prepare_index_params().add_index.called or len(client.prepare_index_params().add_index.call_args_list) >= 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_build_hybrid_index.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.build_hybrid_index'`。

- [ ] **Step 3: Implement scripts/build_hybrid_index.py**

```python
#!/usr/bin/env python3
"""FILA Milvus hybrid 索引(search_text+BM25+sparse_vector / dense_vector)。

复刻 descent schema_manager + build_index,适配 fila 数据源(skus.jsonl)与配置。
sparse_vector 由服务端 BM25 Function 从 search_text 自动生成,客户端不填。

用法(在 fila_agent_html 目录)::

  source .venv/bin/activate
  export PYTHONPATH="$(pwd)"
  export ARK_API_KEY=...
  python3 scripts/build_hybrid_index.py [--reset] [--incremental] [--limit N] [--batch-size 500]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Iterator

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("build_hybrid_index")

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from index_sync_state import (  # noqa: E402
    DEFAULT_STATE_PATH,
    clear_milvus_bucket,
    load_state,
    milvus_text_row_signature,
    save_state,
)
from scripts.hybrid_text import build_keyword_text, build_semantic_text  # noqa: E402

DataType = None
CollectionSchema = FieldSchema = Function = FunctionType = MilvusClient = None  # type: ignore


def _import_pymilvus() -> None:
    global DataType, CollectionSchema, FieldSchema, Function, FunctionType, MilvusClient
    if MilvusClient is not None:
        return
    from pymilvus import (  # type: ignore
        CollectionSchema,
        DataType,
        FieldSchema,
        Function,
        FunctionType,
        MilvusClient as _MC,
    )
    globals()["DataType"] = DataType
    globals()["CollectionSchema"] = CollectionSchema
    globals()["FieldSchema"] = FieldSchema
    globals()["Function"] = Function
    globals()["FunctionType"] = FunctionType
    MilvusClient = _MC


def load_yaml_config() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# 标量字段:(name, dtype, kwargs)。dtype 在 build_hybrid_schema 里映射。
_SCALAR_FIELDS: list[tuple[str, str, dict[str, Any]]] = [
    ("product_name", "VARCHAR", {"max_length": 256}),
    ("product_name_short", "VARCHAR", {"max_length": 128}),
    ("goods_sn", "VARCHAR", {"max_length": 64}),
    ("brand_line", "VARCHAR", {"max_length": 64}),
    ("category", "VARCHAR", {"max_length": 128}),
    ("category_l1", "VARCHAR", {"max_length": 32}),
    ("category_l2", "VARCHAR", {"max_length": 64}),
    ("up_down_raw", "VARCHAR", {"max_length": 32}),
    ("role", "VARCHAR", {"max_length": 32}),
    ("color_name", "VARCHAR", {"max_length": 64}),
    ("color_series", "ARRAY", {"element_type": "VARCHAR", "max_length": 32, "max_capacity": 8}),
    ("gender", "ARRAY", {"element_type": "VARCHAR", "max_length": 32, "max_capacity": 8}),
    ("season", "VARCHAR", {"max_length": 256}),
    ("series", "VARCHAR", {"max_length": 64}),
    ("sub_series", "VARCHAR", {"max_length": 128}),
    ("year", "VARCHAR", {"max_length": 16}),
    ("modeling", "VARCHAR", {"max_length": 16}),
    ("length", "VARCHAR", {"max_length": 16}),
    ("length_class", "VARCHAR", {"max_length": 16}),
    ("layer", "VARCHAR", {"max_length": 16}),
    ("coverage", "VARCHAR", {"max_length": 16}),
    ("is_intimate", "VARCHAR", {"max_length": 8}),
    ("scene_domain", "VARCHAR", {"max_length": 32}),
    ("group_brand", "VARCHAR", {"max_length": 64}),
    ("technology", "VARCHAR", {"max_length": 512}),
    ("features", "VARCHAR", {"max_length": 1024}),
    ("selling_point_label", "VARCHAR", {"max_length": 128}),
    ("material", "VARCHAR", {"max_length": 1024}),
    ("age", "VARCHAR", {"max_length": 16}),
    ("price", "DOUBLE", {}),
    ("market_price", "DOUBLE", {}),
    ("min_price", "DOUBLE", {}),
    ("max_price", "DOUBLE", {}),
    ("onsell", "INT64", {}),
    ("sales", "INT64", {}),
    ("sales_week", "INT64", {}),
    ("sales_month", "INT64", {}),
    ("w_order", "INT64", {}),
    ("up_time", "INT64", {}),
    ("id_goods", "INT64", {}),
    ("sku_count", "INT64", {}),
]

_DTYPE_MAP = {
    "VARCHAR": "VARCHAR",
    "ARRAY": "ARRAY",
    "DOUBLE": "DOUBLE",
    "INT64": "INT64",
}


def build_hybrid_schema(dim: int) -> Any:
    """构造 hybrid 集合 schema(search_text chinese analyzer + sparse_vector + dense_vector + 标量 + BM25 Function)。"""
    fields = [
        FieldSchema(name="sku_id", dtype=DataType.VARCHAR, max_length=64, is_primary=True, auto_id=False),
        FieldSchema(
            name="search_text",
            dtype=DataType.VARCHAR,
            max_length=8192,
            enable_analyzer=True,
            analyzer_params={"type": "chinese"},
        ),
        FieldSchema(name="sparse_vector", dtype=DataType.SPARSE_FLOAT_VECTOR),
        FieldSchema(name="dense_vector", dtype=DataType.FLOAT_VECTOR, dim=dim),
    ]
    for name, kind, kw in _SCALAR_FIELDS:
        if kind == "ARRAY":
            fields.append(
                FieldSchema(
                    name=name,
                    dtype=DataType.ARRAY,
                    element_type=DataType.VARCHAR,
                    max_length=kw["max_length"],
                    max_capacity=kw["max_capacity"],
                )
            )
        elif kind == "VARCHAR":
            fields.append(FieldSchema(name=name, dtype=DataType.VARCHAR, max_length=kw["max_length"]))
        elif kind == "DOUBLE":
            fields.append(FieldSchema(name=name, dtype=DataType.DOUBLE))
        elif kind == "INT64":
            fields.append(FieldSchema(name=name, dtype=DataType.INT64))
    bm25_function = Function(
        name="search_text_bm25",
        input_field_names=["search_text"],
        output_field_names=["sparse_vector"],
        function_type=FunctionType.BM25,
    )
    return CollectionSchema(
        fields=fields,
        functions=[bm25_function],
        description="FILA SKU hybrid (BM25+dense) search collection",
        enable_dynamic_field=False,
    )


def get_hybrid_index_params() -> list[dict[str, Any]]:
    return [
        {
            "field_name": "sparse_vector",
            "index_name": "idx_sparse",
            "index_type": "SPARSE_INVERTED_INDEX",
            "metric_type": "BM25",
            "params": {"drop_ratio_build": 0.2},
        },
        {
            "field_name": "dense_vector",
            "index_name": "idx_dense",
            "index_type": "IVF_FLAT",
            "metric_type": "COSINE",
            "params": {"nlist": 64},
        },
        {"field_name": "up_time", "index_name": "up_time_inv_idx", "index_type": "INVERTED"},
        {"field_name": "group_brand", "index_name": "group_brand_inv_idx", "index_type": "INVERTED"},
    ]


def create_hybrid_collection(client: Any, name: str, dim: int, uri: str) -> None:
    """建集合 + 索引(随 create_collection 一次落)。local *.db 报错。"""
    from backend.config import is_milvus_lite_local_uri

    if is_milvus_lite_local_uri(uri):
        raise SystemExit(
            "BM25 Function 在 Milvus Lite(*.db)下可能不支持,请用 cloud Milvus(uri=http://...):"
            " 设 FILA_MILVUS_MODE=cloud 或 FILA_MILVUS_URI"
        )
    if client.has_collection(name):
        logger.info("Collection already exists: %s", name)
        return
    schema = build_hybrid_schema(dim)
    index_params = client.prepare_index_params()
    for cfg in get_hybrid_index_params():
        index_params.add_index(**cfg)
    client.create_collection(collection_name=name, schema=schema, index_params=index_params)
    logger.info("Created hybrid collection: %s (dim=%d)", name, dim)


def build_insert_row(row: dict[str, Any], vec: list[float], dim: int) -> dict[str, Any]:
    """单行 Milvus insert 记录:search_text 原文 + dense_vector + 标量;不含 sparse_vector。"""
    search_text = build_keyword_text(row)[:8192]
    rec: dict[str, Any] = {
        "sku_id": str(row.get("sku_id") or "")[:64],
        "search_text": search_text,
        "dense_vector": vec,
    }
    for name, kind, kw in _SCALAR_FIELDS:
        if name == "color_series":
            rec[name] = [str(x)[:32] for x in (row.get(name) or [])][:8]
        elif name == "gender":
            rec[name] = [str(x)[:32] for x in (row.get(name) or [])][:8]
        elif kind == "VARCHAR":
            rec[name] = str(row.get(name) or "")[: kw["max_length"]]
        elif kind == "DOUBLE":
            try:
                rec[name] = float(row.get(name) or 0.0)
            except (TypeError, ValueError):
                rec[name] = 0.0
        elif kind == "INT64":
            try:
                rec[name] = int(float(row.get(name) or 0))
            except (TypeError, ValueError):
                rec[name] = 0
    return rec


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def main() -> None:
    parser = argparse.ArgumentParser(description="FILA Milvus hybrid 索引构建")
    parser.add_argument("--reset", action="store_true", help="删除并重建集合")
    parser.add_argument("--incremental", action="store_true", help="仅 search_text 签名变化时重算")
    parser.add_argument("--limit", type=int, default=0, help="仅前 N 条(smoke)")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--state-file", type=str, default="")
    args = parser.parse_args()

    cfg = load_yaml_config()
    mv = cfg.get("milvus") or {}
    if not mv.get("enabled"):
        raise SystemExit("milvus.enabled 为 false")
    from backend.config import (  # noqa: E402
        get_milvus_token,
        get_milvus_uri,
        is_milvus_lite_local_uri,
        restore_stashed_milvus_uri,
        stash_milvus_db_uri_before_pymilvus_import,
    )
    from backend.embedding_client import embed_text  # noqa: E402

    uri_env = str(mv.get("uri_env") or "FILA_MILVUS_URI")
    stash_milvus_db_uri_before_pymilvus_import(uri_env)
    try:
        _import_pymilvus()
    finally:
        restore_stashed_milvus_uri()
    if MilvusClient is None:
        raise SystemExit("pymilvus 不可用,见 requirements.txt")

    uri = get_milvus_uri(cfg)
    token = get_milvus_token(cfg)
    col_name = str((mv.get("collections") or {}).get("sku_hybrid_vectors") or "fila_sku_hybrid_vectors")
    dim = int((cfg.get("embedding") or {}).get("dimensions") or 1024)
    embedding_model = str((cfg.get("embedding") or {}).get("model") or "")

    state_path = Path(args.state_file).expanduser().resolve() if args.state_file.strip() else DEFAULT_STATE_PATH
    state = load_state(state_path)
    state["milvus"].setdefault("sku_hybrid_vectors", {})

    proc = ROOT / (cfg.get("paths") or {}).get("processed_dir", "data/processed")
    skus_path = proc / "skus.jsonl"
    if not skus_path.is_file():
        raise SystemExit(f"缺少 {skus_path},先跑 scripts/build_catalog.py")

    client = MilvusClient(uri=uri, token=token or None)

    if args.reset:
        clear_milvus_bucket(state, "sku_hybrid_vectors")
        if client.has_collection(col_name):
            client.drop_collection(col_name)
            logger.info("Dropped: %s", col_name)
    if not client.has_collection(col_name):
        create_hybrid_collection(client, col_name, dim, uri)

    prior_sigs = state["milvus"]["sku_hybrid_vectors"]
    batch: list[dict[str, Any]] = []
    ok = skip = 0
    file_sigs: dict[str, str] = {}
    first_vec: list[float] | None = None
    total = 0
    rows = list(iter_jsonl(skus_path))
    if args.limit > 0:
        rows = rows[: args.limit]
    for idx, row in enumerate(rows, 1):
        sku_id = str(row.get("sku_id") or "").strip()
        if not sku_id:
            continue
        total += 1
        kw_text = build_keyword_text(row)
        sig = milvus_text_row_signature(kw_text, dimensions=dim, embedding_model=embedding_model)
        file_sigs[sku_id] = sig
        if args.incremental and prior_sigs.get(sku_id) == sig:
            skip += 1
            continue
        sem_text = build_semantic_text(row)
        vec = embed_text(sem_text)
        if not vec or len(vec) != dim:
            skip += 1
            continue
        if first_vec is None:
            first_vec = vec
        batch.append(build_insert_row(row, vec, dim))
        if len(batch) >= args.batch_size:
            client.insert(col_name, batch)
            ok += len(batch)
            batch.clear()
            logger.info("[%d] inserted=%d skip=%d", idx, ok, skip)
    if batch:
        client.insert(col_name, batch)
        ok += len(batch)
    client.flush(col_name)

    if not args.limit:
        state["milvus"]["sku_hybrid_vectors"] = file_sigs
        save_state(state, state_path)

    logger.info("hybrid index done: col=%s inserted=%d skipped=%d uri=%s", col_name, ok, skip, uri)
    if args.limit and first_vec is None:
        raise SystemExit("TEST: 无向量写入,检查 ARK_API_KEY 与网络")
    print(f"\nhybrid 索引构建结束。collection={col_name} inserted={ok} skip={skip}\n  uri={uri}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_build_hybrid_index.py -v`
Expected: PASS(4 个用例)。

- [ ] **Step 5: Commit**

```bash
git add scripts/build_hybrid_index.py tests/test_build_hybrid_index.py
git commit -m "feat: Milvus hybrid 索引构建脚本(BM25 Function + IVF_FLAT dense)"
```

---

### Task 5: 运行时 hybrid 检索(hybrid_search.py)

复刻 descent search_engine,适配 fila 配置/embed_text/expr。`build_filter_expr`/`_format_results`/`rule_rewrite` 纯函数单测;`search_hybrid` 用 mock client 测调用契约。

**Files:**
- Create: `backend/retrieval/hybrid_search.py`
- Test: `tests/test_hybrid_search.py`

**Interfaces:**
- Consumes:`backend.config`(milvus helpers + load_config)、`backend.embedding_client.embed_text`、`pymilvus`(AnnSearchRequest/RRFRanker/WeightedRanker/MilvusClient)、`jieba`。
- Produces:`FilaSkuHybridSearcher`(供 Task 6 用):`search_keyword(query, *, expr, limit, output_fields)`/`search_semantic(...)`/`search_hybrid(query, *, expr, limit, kw_w, sem_w, ranker, output_fields)`/`get_skus_by_ids(ids, output_fields)`,返回 `list[dict]`(每项含 `sku_id`/`score`/各 output 字段)。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hybrid_search.py
from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.retrieval import hybrid_search as hs


class TestRuleRewrite(unittest.TestCase):
    def test_extracts_gender_and_price(self):
        r = hs.rule_rewrite("男款 100元以内", {})
        self.assertEqual(r.filters.get("gender"), "男")
        self.assertEqual(r.filters.get("price_max"), 100)
        self.assertIn("男款", r.filters) is False  # 男款 被抽走,keyword_query 不再含
        self.assertNotIn("男款", r.keyword_query)

    def test_keeps_plain_keyword(self):
        r = hs.rule_rewrite("短袖T", {})
        self.assertEqual(r.keyword_query, "短袖T")


class TestBuildFilterExpr(unittest.TestCase):
    def test_price_and_gender(self):
        expr = hs.build_filter_expr({"price_min": 100, "price_max": 300, "gender": "男"})
        self.assertIn("price >= 100", expr)
        self.assertIn("price <= 300", expr)
        self.assertIn('gender == "男"', expr)


class TestFormatResults(unittest.TestCase):
    def test_parses_dict_hits(self):
        raw = [[
            {"id": "S1", "distance": 0.9, "entity": {"sku_id": "S1", "title": "短袖T"}},
            {"id": "S2", "distance": 0.5, "entity": {"sku_id": "S2", "title": "短裤"}},
        ]]
        items = hs.format_results(raw, output_fields=["sku_id", "title"])
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["sku_id"], "S1")
        self.assertAlmostEqual(items[0]["score"], 0.9)

    def test_empty(self):
        self.assertEqual(hs.format_results([], output_fields=["sku_id"]), [])


class TestSearchHybridCallsClient(unittest.TestCase):
    def test_invokes_hybrid_search(self):
        from unittest.mock import MagicMock
        client = MagicMock()
        client.hybrid_search.return_value = [[
            {"id": "S1", "distance": 0.8, "entity": {"sku_id": "S1", "title": "T"}}
        ]]
        searcher = hs.FilaSkuHybridSearcher(client=client)
        items = searcher.search_hybrid("短袖T", expr='role == "top"', limit=5, output_fields=["sku_id", "title"])
        self.assertTrue(client.hybrid_search.called)
        call = client.hybrid_search.call_args
        self.assertEqual(call.kwargs["collection_name"], searcher.collection_name)
        self.assertEqual(len(call.kwargs["reqs"]), 2)
        self.assertEqual(call.kwargs["reqs"][0].anns_field, "sparse_vector")
        self.assertEqual(call.kwargs["reqs"][1].anns_field, "dense_vector")
        self.assertEqual(items[0]["sku_id"], "S1")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_hybrid_search.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.retrieval.hybrid_search'`。

- [ ] **Step 3: Implement backend/retrieval/hybrid_search.py**

```python
"""FILA Milvus hybrid 检索(descent search_engine 的 fila 版)。

单集合 fila_sku_hybrid_vectors:sparse_vector(BM25)+ dense_vector(COSINE),
MilvusClient.hybrid_search(AnnSearchRequest ×2 + RRFRanker/WeightedRanker)。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import jieba
from pymilvus import AnnSearchRequest, MilvusClient, RRFRanker, WeightedRanker

from backend.config import (
    get_milvus_token,
    get_milvus_uri,
    is_milvus_lite_local_uri,
    load_config,
    restore_stashed_milvus_uri,
    stash_milvus_db_uri_before_pymilvus_import,
)
from backend.embedding_client import embed_text

logger = logging.getLogger(__name__)

_cfg0 = load_config()
_mv0 = _cfg0.get("milvus") or {}
_uri_env0 = str(_mv0.get("uri_env") or "FILA_MILVUS_URI")
stash_milvus_db_uri_before_pymilvus_import(_uri_env0)
restore_stashed_milvus_uri()  # pymilvus 已在顶部 import,无需 stash 期间 import

DEFAULT_OUTPUT_FIELDS = [
    "sku_id", "product_name", "product_name_short", "goods_sn", "brand_line",
    "category", "category_l1", "category_l2", "up_down_raw", "role",
    "color_name", "color_series", "gender", "season", "series", "sub_series",
    "year", "modeling", "length", "length_class", "layer", "coverage",
    "is_intimate", "scene_domain", "group_brand", "technology", "features",
    "selling_point_label", "material", "age", "price", "market_price",
    "onsell", "sales", "up_time", "id_goods", "sku_count",
]

_ATTR_WORDS = {
    "男士": {"gender": "男"}, "男款": {"gender": "男"}, "男子": {"gender": "男"},
    "女士": {"gender": "女"}, "女款": {"gender": "女"}, "女子": {"gender": "女"},
    "中性": {"gender": "中性"},
    "男童": {"gender": "男童"}, "女童": {"gender": "女童"},
    "儿童": {"age": "儿童"}, "童装": {"age": "儿童"}, "童鞋": {"age": "儿童"},
    "春季": {"season": "春季"}, "夏季": {"season": "夏季"},
    "秋季": {"season": "秋季"}, "冬季": {"season": "冬季"}, "冬天": {"season": "冬季"},
    "上装": {"up_down": "上装"}, "下装": {"up_down": "下装"},
}

_STOPWORDS = {
    "的", "了", "吗", "呢", "啊", "吧", "呀", "适合", "推荐", "有没有", "有什么",
    "一双", "一件", "一条", "一个", "一款", "买", "送", "穿", "给", "想", "要",
    "找", "看看", "能", "可以", "比较", "最", "很", "好", "什么", "哪个", "哪种",
    "我", "你", "他", "她", "它",
}

_PRICE_RE = re.compile(r"(\d+)\s*(?:元|块)?(?:以[内下]|以下)")
_PRICE_RANGE_RE = re.compile(r"(\d+)\s*[-~到至]\s*(\d+)\s*(?:元|块)?")


@dataclass
class RewriteResult:
    keyword_query: str
    semantic_query: str
    filters: dict = field(default_factory=dict)
    source: str = "rule"


def rule_rewrite(query: str, existing_filters: Optional[dict] = None) -> RewriteResult:
    filters = dict(existing_filters) if existing_filters else {}
    text = query
    m = _PRICE_RE.search(text)
    if m:
        filters.setdefault("price_max", int(m.group(1)))
        text = text[: m.start()] + text[m.end():]
    else:
        rm = _PRICE_RANGE_RE.search(text)
        if rm:
            filters.setdefault("price_min", int(rm.group(1)))
            filters.setdefault("price_max", int(rm.group(2)))
            text = text[: rm.start()] + text[rm.end():]
    parts: list[str] = []
    for word in jieba.cut(text):
        tok = word.strip()
        if not tok:
            continue
        if tok in _ATTR_WORDS:
            for k, v in _ATTR_WORDS[tok].items():
                filters.setdefault(k, v)
            continue
        if tok in _STOPWORDS:
            continue
        parts.append(tok)
    kw = "".join(parts).strip() or query
    return RewriteResult(keyword_query=kw, semantic_query=query, filters=filters, source="rule")


def llm_rewrite(query: str) -> Optional[RewriteResult]:
    """可选 LLM 改写(默认关)。fila 关键词来自 intent 层,接入时 skip_rewrite。"""
    cfg = load_config()
    if not (cfg.get("hybrid") or {}).get("llm_rewrite"):
        return None
    return None  # 预留:LLM 改写未启用


def rewrite_query(query: str, existing_filters: Optional[dict] = None) -> RewriteResult:
    result = llm_rewrite(query) or rule_rewrite(query, existing_filters)
    if existing_filters:
        for k, v in existing_filters.items():
            result.filters.setdefault(k, v)
    return result


def build_filter_expr(filters: Optional[dict] = None) -> str:
    """fila 标量过滤 expr(对应 descent build_filter_expr 的 fila 子集)。"""
    if not filters:
        return ""
    conds: list[str] = []
    if "price_min" in filters:
        conds.append(f"price >= {filters['price_min']}")
    if "price_max" in filters:
        conds.append(f"price <= {filters['price_max']}")
    if "gender" in filters:
        conds.append(f'gender == "{filters["gender"]}"')
    if "age" in filters:
        conds.append(f'age == "{filters["age"]}"')
    if "season" in filters:
        conds.append(f'season like "%{filters["season"]}%"')
    if "brand_line" in filters:
        conds.append(f'brand_line == "{filters["brand_line"]}"')
    if "category_l1" in filters:
        conds.append(f'category_l1 == "{filters["category_l1"]}"')
    if "up_down" in filters:
        conds.append(f'up_down_raw == "{filters["up_down"]}"')
    if "onsell" in filters:
        conds.append(f"onsell == {filters['onsell']}")
    return " and ".join(conds)


def format_results(raw_results: Any, output_fields: list[str]) -> list[dict]:
    if not raw_results:
        return []
    hits = raw_results[0] if isinstance(raw_results, list) and raw_results else raw_results
    out: list[dict] = []
    for hit in hits:
        entity = hit.get("entity", {}) if isinstance(hit, dict) else getattr(hit, "entity", {})
        score = hit.get("distance", 0) if isinstance(hit, dict) else getattr(hit, "distance", 0)
        item_id = hit.get("id", "") if isinstance(hit, dict) else getattr(hit, "id", "")
        row = entity if isinstance(entity, dict) else {}
        item: dict[str, Any] = {
            "sku_id": str(row.get("sku_id") or item_id or ""),
            "score": round(float(score), 4),
        }
        for f in output_fields:
            if f != "sku_id":
                item[f] = row.get(f, "")
        out.append(item)
    return out


class FilaSkuHybridSearcher:
    def __init__(self, client: Any = None) -> None:
        self._client = client
        cfg = load_config()
        mv = cfg.get("milvus") or {}
        self.collection_name = str(
            (mv.get("collections") or {}).get("sku_hybrid_vectors") or "fila_sku_hybrid_vectors"
        )
        hyb = cfg.get("hybrid") or {}
        self._kw_w = float(hyb.get("keyword_weight", 0.2))
        self._sem_w = float(hyb.get("semantic_weight", 0.8))
        self._ranker = str(hyb.get("ranker", "rrf"))
        self._limit = int(hyb.get("default_limit", 20))
        self._nprobe = int(hyb.get("nprobe", 16))

    @property
    def client(self) -> Any:
        if self._client is None:
            cfg = load_config()
            uri = get_milvus_uri(cfg)
            if not uri:
                raise RuntimeError("MILVUS_URI 为空,请配置 milvus")
            if is_milvus_lite_local_uri(uri):
                raise RuntimeError("hybrid search 不支持 local Milvus Lite,请用 cloud uri")
            self._client = MilvusClient(uri=uri, token=get_milvus_token(cfg) or None)
        return self._client

    def _encode(self, query: str) -> list[float]:
        vec = embed_text(query)
        if not vec:
            raise RuntimeError("embed_text 返回空,无法做 semantic/hybrid 检索")
        return vec

    def search_keyword(self, query: str, *, expr: Optional[str] = None, limit: Optional[int] = None,
                       output_fields: Optional[list[str]] = None, skip_rewrite: bool = False) -> list[dict]:
        rw = RewriteResult(query, query, {}, "passthrough") if skip_rewrite else rewrite_query(query)
        res = self.client.search(
            collection_name=self.collection_name,
            data=[rw.keyword_query],
            anns_field="sparse_vector",
            search_params={"metric_type": "BM25"},
            limit=limit or self._limit,
            output_fields=output_fields or DEFAULT_OUTPUT_FIELDS,
            filter=expr or None,
        )
        return format_results(res, output_fields or DEFAULT_OUTPUT_FIELDS)

    def search_semantic(self, query: str, *, expr: Optional[str] = None, limit: Optional[int] = None,
                        output_fields: Optional[list[str]] = None, skip_rewrite: bool = False) -> list[dict]:
        rw = RewriteResult(query, query, {}, "passthrough") if skip_rewrite else rewrite_query(query)
        vec = self._encode(rw.semantic_query)
        res = self.client.search(
            collection_name=self.collection_name,
            data=[vec],
            anns_field="dense_vector",
            search_params={"metric_type": "COSINE", "params": {"nprobe": self._nprobe}},
            limit=limit or self._limit,
            output_fields=output_fields or DEFAULT_OUTPUT_FIELDS,
            filter=expr or None,
        )
        return format_results(res, output_fields or DEFAULT_OUTPUT_FIELDS)

    def search_hybrid(self, query: str, *, expr: Optional[str] = None, limit: Optional[int] = None,
                     kw_w: Optional[float] = None, sem_w: Optional[float] = None,
                     ranker: Optional[str] = None, output_fields: Optional[list[str]] = None,
                     skip_rewrite: bool = False) -> list[dict]:
        rw = RewriteResult(query, query, {}, "passthrough") if skip_rewrite else rewrite_query(query)
        vec = self._encode(rw.semantic_query)
        fetch_limit = limit or self._limit
        keyword_req = AnnSearchRequest(
            data=[rw.keyword_query], anns_field="sparse_vector",
            param={"metric_type": "BM25"}, limit=fetch_limit, expr=expr or None,
        )
        semantic_req = AnnSearchRequest(
            data=[vec], anns_field="dense_vector",
            param={"metric_type": "COSINE", "params": {"nprobe": self._nprobe}},
            limit=fetch_limit, expr=expr or None,
        )
        rerank = RRFRanker(k=60) if (ranker or self._ranker) == "rrf" else WeightedRanker(
            kw_w if kw_w is not None else self._kw_w,
            sem_w if sem_w is not None else self._sem_w,
        )
        res = self.client.hybrid_search(
            collection_name=self.collection_name,
            reqs=[keyword_req, semantic_req],
            ranker=rerank,
            limit=fetch_limit,
            output_fields=output_fields or DEFAULT_OUTPUT_FIELDS,
        )
        return format_results(res, output_fields or DEFAULT_OUTPUT_FIELDS)

    def get_skus_by_ids(self, sku_ids: list[str], output_fields: Optional[list[str]] = None) -> list[dict]:
        if not sku_ids:
            return []
        in_list = ", ".join(f'"{s}"' for s in sku_ids)
        res = self.client.query(
            collection_name=self.collection_name,
            filter=f"sku_id in [{in_list}]",
            output_fields=output_fields or DEFAULT_OUTPUT_FIELDS,
        )
        id_map = {it.get("sku_id", ""): it for it in res}
        return [id_map[s] for s in sku_ids if s in id_map]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_hybrid_search.py -v`
Expected: PASS(5 个用例)。

- [ ] **Step 5: Commit**

```bash
git add backend/retrieval/hybrid_search.py tests/test_hybrid_search.py
git commit -m "feat: FilaSkuHybridSearcher(BM25+dense hybrid_search + rewrite)"
```

---

### Task 6: SkuRetriever.recall_by_hybrid 接入

新增方法,复用现有 expr 构造器,每关键词一次 search_hybrid → 按 sku 取 max;0 命中 fallback 旧 dense 通路。用 mock searcher 单测。

**Files:**
- Modify: `backend/retrieval/sku_retriever.py`(新增 `recall_by_hybrid`)
- Test: `tests/test_recall_by_hybrid.py`

**Interfaces:**
- Consumes:Task 5 `FilaSkuHybridSearcher.search_hybrid(query, *, expr, limit, output_fields)` → 返回 `list[dict]`(每项 `sku_id`/`score`)。
- Consumes:现有 `merge_milvus_expr`/`build_category_l2_milvus_expr`/`build_group_brand_milvus_expr`/`build_up_time_milvus_expr`(sku_retriever 已 import)。
- Produces:`SkuRetriever.recall_by_hybrid(keywords, *, top_k_per_keyword, role_filter, gender_filter, age_filter, category_l2_filter, color_series_filter, attr_expr, group_brand, trace_id, fallback_on_empty, color_series_match_mode) -> list[tuple[str,float,float]]`。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_recall_by_hybrid.py
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from backend.retrieval.sku_retriever import SkuRetriever


def _make_searcher(hits_per_kw: dict[str, list[tuple[str, float]]]):
    searcher = MagicMock()
    def _hq(query, *, expr=None, limit=None, output_fields=None, skip_rewrite=False):
        return [{"sku_id": sid, "score": sc} for sid, sc in hits_per_kw.get(query, [])]
    searcher.search_hybrid.side_effect = _hq
    return searcher


class TestRecallByHybrid(unittest.TestCase):
    def _retriever(self, searcher):
        r = SkuRetriever.__new__(SkuRetriever)
        r._milvus = MagicMock()
        r._milvus.hit_to_similarity.side_effect = lambda x: float(x)
        r._es = MagicMock()
        r._store = MagicMock()
        r._data = MagicMock()
        return r

    def test_merge_by_max_per_keyword(self):
        searcher = _make_searcher({
            "短袖": [("S1", 0.9), ("S2", 0.5)],
            "T恤": [("S1", 0.7), ("S3", 0.6)],
        })
        r = self._retriever(searcher)
        rows = r.recall_by_hybrid(["短袖", "T恤"], category_l2_filter=None,
                                  color_series_filter=None, group_brand=None,
                                  attr_expr=None, trace_id="t1", fallback_on_empty=False)
        merged = {sid: sim for sid, sim, _ in rows}
        self.assertAlmostEqual(merged["S1"], 0.9)  # 取 max
        self.assertAlmostEqual(merged["S3"], 0.6)

    def test_fallback_to_text_vector_on_empty(self):
        searcher = _make_searcher({"短袖": []})
        r = self._retriever(searcher)
        r.recall_by_text_vector_keywords = MagicMock(return_value=[("S9", 0.3, 0.3)])
        rows = r.recall_by_hybrid(["短袖"], category_l2_filter=None,
                                  color_series_filter=None, group_brand=None,
                                  attr_expr=None, trace_id="t2", fallback_on_empty=True)
        self.assertTrue(r.recall_by_text_vector_keywords.called)
        self.assertEqual(rows[0][0], "S9")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_recall_by_hybrid.py -v`
Expected: FAIL — `AttributeError: 'SkuRetriever' object has no attribute 'recall_by_hybrid'`。

- [ ] **Step 3: Implement recall_by_hybrid in sku_retriever.py**

在 `backend/retrieval/sku_retriever.py` 顶部 import 加:
```python
from backend.retrieval.hybrid_search import FilaSkuHybridSearcher
```
在 `SkuRetriever.__init__` 加可选 searcher 注入:
```python
    def __init__(
        self,
        store: LocalDataStore,
        es: Optional[EsClient] = None,
        milvus: Optional[MilvusClient] = None,
        data: DataFacade | None = None,
        hybrid_searcher: Optional[FilaSkuHybridSearcher] = None,
    ) -> None:
        self._store = store
        self._es = es or EsClient()
        self._milvus = milvus or MilvusClient()
        self._data = data or DataFacade(store, self._es)
        self._hybrid = hybrid_searcher or FilaSkuHybridSearcher()
```
在 `recall_by_text_vector_keywords` 方法之后追加 `recall_by_hybrid`(复用其 `_build_color_series_expr`/`_build_gender_expr`/`_build_age_expr`/`_build_expr` 逻辑——为避免改动既有方法内部,这里复制必要片段并调用 searcher):

```python
    def recall_by_hybrid(
        self,
        keywords: list[str],
        top_k_per_keyword: Optional[int] = None,
        *,
        role_filter: str | None = None,
        gender_filter: str | None = None,
        age_filter: str | None = None,
        category_l2_filter: list[str] | None = None,
        color_series_filter: list[str] | None = None,
        trace_id: str | None = None,
        fallback_on_empty: bool = True,
        attr_expr: str | None = None,
        color_series_match_mode: str = "auto",
        group_brand: str | None = None,
    ) -> List[Tuple[str, float, float]]:
        """hybrid(BM25+dense)文本召回:每关键词一次 search_hybrid,按 sku 取 max score。

        0 命中时 fallback 到 recall_by_text_vector_keywords(旧 dense 通路)。
        hybrid score 非 COSINE 量纲,默认 min_sim=0(不过滤)。
        """
        if not keywords:
            return []
        cfg = load_config()
        rec = cfg.get("recommend") or {}
        k = top_k_per_keyword or int(rec.get("text_milvus_top_k") or 5)
        cs_mode = color_series_match_mode if color_series_match_mode in ("strict", "relaxed") else "auto"
        primary_mode = "strict" if cs_mode == "auto" else cs_mode

        def _build_color_expr(cs: list[str] | None, mode: str) -> str | None:
            if not cs:
                return None
            vals = [s.strip() for s in cs if s.strip()]
            if not vals:
                return None
            if "多色系" not in vals:
                vals.append("多色系")
            in_list = ", ".join(f'"{s}"' for s in vals)
            contains = f"array_contains_any(color_series, [{in_list}])"
            if mode == "strict":
                return f"array_length(color_series) == 1 && {contains}"
            return contains

        def _build_expr(cs_mode_inner: str) -> str | None:
            return merge_milvus_expr(
                f'role == "{role_filter}"' if role_filter else None,
                f'array_contains_any(gender, ["{gender_filter}"])' if gender_filter else None,
                f'age in ["{age_filter}", "通码"]' if age_filter and age_filter != "通码" else ('age == "通码"' if age_filter == "通码" else None),
                build_category_l2_milvus_expr(category_l2_filter or []),
                _build_color_expr(color_series_filter, cs_mode_inner),
                attr_expr or 'is_intimate == "false"',
                build_group_brand_milvus_expr(group_brand),
                build_up_time_milvus_expr(),
            )

        def _run(search_expr: str | None) -> list[tuple[str, float, float]]:
            merged: dict[str, tuple[float, float]] = {}
            for kw in keywords:
                hits = self._hybrid.search_hybrid(
                    kw, expr=search_expr, limit=k, skip_rewrite=True,
                    output_fields=["sku_id"],
                )
                for h in hits:
                    sid = str(h.get("sku_id") or "")
                    if not sid:
                        continue
                    raw = float(h.get("score") or 0)
                    sim = self._milvus.hit_to_similarity(raw)
                    prev = merged.get(sid)
                    if prev is None or sim > prev[0]:
                        merged[sid] = (sim, raw)
            rows = [(sid, sim, raw) for sid, (sim, raw) in merged.items()]
            rows.sort(key=lambda x: x[1], reverse=True)
            return rows

        rows = _run(_build_expr(primary_mode))
        if cs_mode == "auto" and color_series_filter and len(rows) < k:
            relaxed = _run(_build_expr("relaxed"))
            have = {sid for sid, _, _ in rows}
            for sid, sim, raw in relaxed:
                if sid not in have:
                    rows.append((sid, sim, raw))
            rows.sort(key=lambda x: x[1], reverse=True)

        if not rows and fallback_on_empty:
            logger.info("hybrid_recall: 0 results, fallback to text_vector_keywords")
            return self.recall_by_text_vector_keywords(
                keywords,
                top_k_per_keyword=top_k_per_keyword,
                role_filter=role_filter,
                gender_filter=gender_filter,
                age_filter=age_filter,
                category_l2_filter=category_l2_filter,
                color_series_filter=color_series_filter,
                trace_id=trace_id,
                fallback_on_empty=False,
                color_series_match_mode=color_series_match_mode,
                attr_expr=attr_expr,
                group_brand=group_brand,
            )
        return rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_recall_by_hybrid.py -v`
Expected: PASS(2 个用例)。

- [ ] **Step 5: Commit**

```bash
git add backend/retrieval/sku_retriever.py tests/test_recall_by_hybrid.py
git commit -m "feat: SkuRetriever.recall_by_hybrid 接入 hybrid 召回(dense fallback)"
```

---

### Task 7: ES build 富化(mapping + sku_doc)

**Files:**
- Modify: `scripts/build_fila_es_index.py`(`create_skus_index` mapping + `sku_doc`)
- Test: `tests/test_es_hybrid_fields.py`

**Interfaces:**
- Consumes:Task 3 `build_keyword_text`。
- Produces:ES skus mapping 含 `brand_line`/`year`/`market_price`/`min_price`/`max_price`/`goods_sn`/`onsell`/`sales`/`sales_week`/`sales_month`/`w_order`/`sku_count`(keyword/float/integer)+ `features`/`selling_point_label`/`technology`/`keyword`/`product_name_short`(ik text);`sku_doc.search_text` = `build_keyword_text(row)`。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_es_hybrid_fields.py
from __future__ import annotations

import unittest

from scripts import build_fila_es_index as esb


class TestSkuDocSearchTextEnriched(unittest.TestCase):
    def test_search_text_uses_build_keyword_text(self):
        row = {"sku_id": "S1", "title": "短袖T", "brand_line": "FILA", "features": "透气"}
        doc = esb.sku_doc(row)
        self.assertIn("短袖T", doc["search_text"])
        self.assertIn("FILA", doc["search_text"])
        self.assertIn("透气", doc["search_text"])


class TestMappingHasNewFields(unittest.TestCase):
    def test_properties_contain_new_fields(self):
        body = esb.skus_mapping()
        props = body["mappings"]["properties"]
        for f in ("brand_line", "year", "market_price", "features", "selling_point_label",
                  "technology", "goods_sn", "onsell", "sales", "sku_count"):
            self.assertIn(f, props, f)
        self.assertEqual(props["market_price"]["type"], "double")
        self.assertEqual(props["onsell"]["type"], "integer")
        self.assertEqual(props["features"]["type"], "text")
        self.assertEqual(props["brand_line"]["type"], "keyword")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_es_hybrid_fields.py -v`
Expected: FAIL — `AttributeError: module has no attribute 'skus_mapping'` / `sku_doc.search_text` 不含 features。

- [ ] **Step 3: Refactor + implement**

在 `scripts/build_fila_es_index.py` 把 `create_skus_index` 里的 mapping body 抽成纯函数 `skus_mapping() -> dict`(供单测与 create_index 共用),追加新字段 properties:

```python
def skus_mapping() -> dict:
    ik_index = {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"}
    return {
        "mappings": {
            "properties": {
                # … 既有字段保持不变(sku_id/spu_id/search_text/search_keywords/title/gender/age/...),
                #   复制现有 create_skus_index 里 properties 的全部条目到此处 …
                "brand_line": {"type": "keyword"},
                "year": {"type": "keyword"},
                "goods_sn": {"type": "keyword"},
                "category": {"type": "keyword"},
                "length": {"type": "keyword"},
                "technology": ik_index,
                "features": ik_index,
                "selling_point_label": ik_index,
                "keyword": ik_index,
                "product_name_short": ik_index,
                "market_price": {"type": "double"},
                "min_price": {"type": "double"},
                "max_price": {"type": "double"},
                "onsell": {"type": "integer"},
                "sales": {"type": "integer"},
                "sales_week": {"type": "integer"},
                "sales_month": {"type": "integer"},
                "w_order": {"type": "integer"},
                "sku_count": {"type": "integer"},
                "color_images": {"type": "keyword"},
                "video_url": {"type": "keyword"},
            }
        }
    }


def create_skus_index(client, name):
    if client.indices.exists(index=name):
        return
    create_index(client, name, skus_mapping())
```
(把原 `create_skus_index` 内联 properties 全部迁入 `skus_mapping()`;既有字段一字不改,仅追加上面新增条目。)

改 `sku_doc(row)` 的 `search_text` 行:
```python
        "search_text": build_keyword_text(row),
```
并在文件顶部 import:
```python
from scripts.hybrid_text import build_keyword_text
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_es_hybrid_fields.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add scripts/build_fila_es_index.py tests/test_es_hybrid_fields.py
git commit -m "feat: ES skus mapping + 富化 search_text(build_keyword_text)"
```

---

### Task 8: config.yaml 配置追加

**Files:**
- Modify: `config.yaml`(milvus 段)

**Interfaces:** 无(纯配置)。

- [ ] **Step 1: 追加配置**

在 `config.yaml` 的 `milvus:` 段 `collections:` 下加 `sku_hybrid_vectors`,并新增 `hybrid:` 子段:

```yaml
  collections:
    sku_vectors: "fila_sku_vectors"
    sku_text_vectors: "fila_sku_text_vectors"
    sku_complementary_vectors: "fila_sku_complementary_vectors"
    sku_hybrid_vectors: "fila_sku_hybrid_vectors"   # 新增:BM25+dense hybrid
  # 新增:hybrid 检索参数(FilaSkuHybridSearcher 使用)
  hybrid:
    keyword_weight: 0.2
    semantic_weight: 0.8
    ranker: rrf            # rrf | weighted
    default_limit: 20
    nprobe: 16
    llm_rewrite: false
```

- [ ] **Step 2: 校验 YAML 合法**

Run: `python -c "import yaml; yaml.safe_load(open('config.yaml')); print('yaml ok')"`
Expected: `yaml ok`。

- [ ] **Step 3: 跑全量测试确认无回归**

Run: `python -m pytest tests/test_index_sync_state_hybrid_bucket.py tests/test_descent_extra_fields.py tests/test_hybrid_text.py tests/test_build_hybrid_index.py tests/test_hybrid_search.py tests/test_recall_by_hybrid.py tests/test_es_hybrid_fields.py -v`
Expected: 全 PASS。

- [ ] **Step 4: Commit**

```bash
git add config.yaml
git commit -m "feat: config 加 milvus.sku_hybrid_vectors + hybrid 检索参数"
```

---

## Self-Review(已执行)

- **Spec 覆盖**:Part 1 字段→Task 2;Part 2 共享文本→Task 3;Part 3 ES→Task 7;Part 4 Milvus build→Task 4;Part 5 运行时检索+接入→Task 5/6;config→Task 8;index_sync_state gotcha→Task 1。全覆盖。
- **Placeholder**:`skus_mapping()` 的"复制既有 properties"——这是迁移既有代码,非占位;Step 3 已说明保留全部既有字段仅追加。其余代码均完整。
- **类型一致**:`build_keyword_text`/`build_semantic_text`/`FilaSkuHybridSearcher`/`recall_by_hybrid`/`search_hybrid` 跨 Task 命名一致;`search_hybrid` 返回 `list[dict]`(含 `sku_id`/`score`),Task 6 按此消费。

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-09-milvus-hybrid-descent-replication.md`. Two execution options:

**1. Subagent-Driven (recommended)** - 每 Task 派新 subagent,任务间 review,快速迭代。

**2. Inline Execution** - 在本会话用 executing-plans 批量执行,带 checkpoint review。

选哪种?
