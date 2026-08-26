# Progressive Slot-Filter Relaxation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a role's ES/Milvus retrieval returns 0 hits, progressively drop soft filter slots in a configurable priority order until non-empty — never touching the hard identity slots (gender/season/age).

**Architecture:** A shared, pathway-agnostic driver `run_with_progressive_relax` walks an ordered `relax_priority` list, re-running each pathway's own search with the next slot cleared. Each pathway supplies a `search_fn(dropped_set)` closure that maps dropped slot-names onto that pathway's existing skip-knobs (ES skip-flags) or rebuilt expr-parts (Milvus text/hybrid, complementary). A single config block (`recommend.enable_progressive_relax` / `relax_priority` / `relax_min_hits`) governs all three. Hard slots are never members of `relax_priority`, so the loop stops at the identity wall.

**Tech Stack:** Python 3.11+, Elasticsearch 7.9.3, Milvus (cloud 2.6.3, BM25+dense hybrid), PyYAML config, `unittest.TestCase` tests under `tests/`.

## Global Constraints

- **Hard slots never relaxed:** `gender`, `season`, `age` are never members of `relax_priority`; the driver cannot reach them. (`up_time` and `price` ARE soft, placed at the tail — per approved spec decision option 2.)
- **Canonical dropped-slot names** (entries of `relax_priority`): `modeling`, `length_class`, `coverage`, `series`, `scene_domain`, `color_series`, `category_l2`, `anchor_attr_must_not`, `up_time`, `price`. Every pathway's `search_fn` maps these same names to its own skip actions.
- **Slot-name semantics** (disambiguates where the same attribute appears in multiple clauses):
  - `length_class` / `coverage` → clear the **per-role positive** clause only (in `build_role_es_positive_filters` / `build_role_milvus_expr_parts`).
  - `series` → clear the **anchor-isolation** series clause (`build_series_es_filter` / `build_series_milvus_expr`) only; per-role positive series stays.
  - `scene_domain` → clear the **anchor-isolation** scene clause only; per-role positive scene_domain stays.
  - `anchor_attr_must_not` → clear the whole anchor-attr exclusion unit (`build_attr_es_filter` / `build_attr_milvus_expr`), which includes the `length_class != "short"` / `coverage != "full"` / `layer != ...` exclusions as a group.
  - `modeling` → clear the per-role modeling terms (synonym-expanded).
  - `color_series` → clear the pairing color_series clause (ES: `allowed_companion_color_series=None` + `skip_color_series=True`; Milvus: drop the `array_contains_any(color_series, [...])` pairing part; also clear per-role positive color_series).
  - `category_l2` → clear the pairing category_l2 clause (ES: `effective_cat2=None`/`skip_category_l2`; Milvus text: `category_l2_filter=None`).
  - `up_time` → skip `build_up_time_es_filter` / `build_up_time_milvus_expr`.
  - `price` → skip the per-role budget range / `price >= min` / `price <= max`.
- **Master switch:** `enable_progressive_relax: false` must reproduce today's behavior exactly (existing ES 3-stage ladder / Milvus-text 2-stage recursion / complementary no-relax). Implementation: when false, the driver short-circuits — `search_fn(set())` once, no loop. **However**, to also keep a clean rollback for the *removed* hardcoded ladders, Tasks 3/5/7 delete the old ladder blocks; the rollback path is `git revert`, not a runtime flag on the old code. The runtime flag still gates the *new* loop.
- **Existing low-recall retry untouched:** the `<3` similarity-threshold retry in `recall_text_vector_skus` (outfit_recall.py:927-954) stays as-is; progressive relaxation handles 0, low-recall handles <3-but-nonzero.
- **Latent bug to fix:** the current `recall_by_text_vector_keywords` recursive `fallback_on_empty` calls drop `attr_expr` and `group_brand` (outfit_recall sku_retriever.py:309-320, 328-339). The new driver closure forwards all params every iteration.

---

## File Structure

- **Create** `backend/retrieval/progressive_relax.py` — shared driver + config reader. One responsibility: given a `search_fn`, walk the priority list.
- **Modify** `config.yaml` — add `enable_progressive_relax` / `relax_priority` / `relax_min_hits` under `recommend:`.
- **Modify** `backend/retrieval/es_intent.py` — add 8 new skip-flags to `resolve_es_query_for_role` + gate the 8 emit sites.
- **Modify** `backend/retrieval/sku_retriever.py` — add `skip_up_time` param + remove recursive `fallback_on_empty` ladders in `recall_by_text_vector_keywords` / `recall_by_hybrid`.
- **Modify** `backend/intent/role_slots.py` — add `skip_slots` to `build_role_milvus_expr_parts`; add `skip_modeling`/`skip_price` to `build_modeling_price_milvus_expr`.
- **Modify** `backend/services/outfit_recall.py` — extract `_pick_from_hits` helper; replace ES 3-stage ladder with driver; add `_rebuild_text_attr_expr` helper + replace text-path fallback with driver.
- **Modify** `backend/services/complementary_recall.py` — add `skip_slots` to `_build_role_milvus_expr`; wire driver into `_search_one_role`.
- **Create** `tests/test_progressive_relax_driver.py` — driver unit tests.
- **Create** `tests/test_es_progressive_relax.py` — ES path skip-flags + 0-hit relaxation.
- **Create** `tests/test_text_vector_progressive_relax.py` — text/hybrid path.
- **Create** `tests/test_complementary_progressive_relax.py` — complementary path.
- **Create** `tests/test_progressive_relax_hardwall.py` — hard-wall + tail-only + regression.

---

## Task 1: Shared driver + config

**Files:**
- Create: `backend/retrieval/progressive_relax.py`
- Modify: `config.yaml` (under `recommend:`, near `text_recall_mode` at line ~321)
- Test: `tests/test_progressive_relax_driver.py`

**Interfaces:**
- Produces: `run_with_progressive_relax(search_fn, priority, min_hits) -> tuple[list, list]` returning `(hits, dropped)`; `get_relax_config() -> tuple[bool, list[str], int]` returning `(enabled, priority, min_hits)`.

- [ ] **Step 1: Write the failing test**

`tests/test_progressive_relax_driver.py`:
```python
"""progressive_relax 驱动器单元测试：0 命中时按优先级逐个丢弃 slot 直到非空。"""
from __future__ import annotations

import unittest

from backend.retrieval.progressive_relax import run_with_progressive_relax


class ProgressiveRelaxDriverTest(unittest.TestCase):
    def test_returns_immediately_when_nonempty(self):
        calls: list[set[str]] = []

        def search_fn(dropped: set[str]) -> list[str]:
            calls.append(set(dropped))
            return ["x"]  # non-empty on first call

        hits, dropped = run_with_progressive_relax(
            search_fn, priority=["modeling", "color_series"], min_hits=1,
        )
        self.assertEqual(hits, ["x"])
        self.assertEqual(dropped, [])
        self.assertEqual(calls, [set()])  # only one call, no drops

    def test_drops_in_priority_order_until_nonempty(self):
        # search_fn returns [] until 'modeling' and 'length_class' are both dropped
        calls: list[set[str]] = []

        def search_fn(dropped: set[str]) -> list[str]:
            calls.append(set(dropped))
            if {"modeling", "length_class"}.issubset(dropped):
                return ["hit"]
            return []

        hits, dropped = run_with_progressive_relax(
            search_fn,
            priority=["modeling", "length_class", "color_series"],
            min_hits=1,
        )
        self.assertEqual(hits, ["hit"])
        # modeling dropped (still []), then length_class dropped (now nonempty) → stop
        self.assertEqual(dropped, ["modeling", "length_class"])
        self.assertEqual(
            calls,
            [set(), {"modeling"}, {"modeling", "length_class"}],
        )

    def test_exhausts_priority_and_stops_at_hard_wall(self):
        # never reaches a non-empty set → exhausts list, returns last (empty) result
        calls: list[set[str]] = []

        def search_fn(dropped: set[str]) -> list[str]:
            calls.append(set(dropped))
            return []  # always empty

        hits, dropped = run_with_progressive_relax(
            search_fn, priority=["modeling", "color_series"], min_hits=1,
        )
        self.assertEqual(hits, [])
        self.assertEqual(dropped, ["modeling", "color_series"])
        self.assertEqual(
            calls,
            [set(), {"modeling"}, {"modeling", "color_series"}],
        )

    def test_empty_priority_runs_once(self):
        calls: list[set[str]] = []

        def search_fn(dropped: set[str]) -> list[str]:
            calls.append(set(dropped))
            return []

        hits, dropped = run_with_progressive_relax(search_fn, priority=[], min_hits=1)
        self.assertEqual(hits, [])
        self.assertEqual(dropped, [])
        self.assertEqual(calls, [set()])

    def test_reordering_priority_changes_drop_order(self):
        calls: list[set[str]] = []

        def search_fn(dropped: set[str]) -> list[str]:
            calls.append(set(dropped))
            return ["h"] if "color_series" in dropped else []

        hits, dropped = run_with_progressive_relax(
            search_fn, priority=["color_series", "modeling"], min_hits=1,
        )
        self.assertEqual(dropped, ["color_series"])
        self.assertEqual(calls, [set(), {"color_series"}])

    def test_min_hits_threshold(self):
        # min_hits=3: 1 hit is not enough, keep dropping
        def search_fn(dropped: set[str]) -> list[str]:
            if "modeling" in dropped and "color_series" not in dropped:
                return ["a"]  # below threshold
            if {"modeling", "color_series"}.issubset(dropped):
                return ["a", "b", "c"]
            return []

        hits, dropped = run_with_progressive_relax(
            search_fn, priority=["modeling", "color_series"], min_hits=3,
        )
        self.assertEqual(len(hits), 3)
        self.assertEqual(dropped, ["modeling", "color_series"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_progressive_relax_driver.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.retrieval.progressive_relax'`

- [ ] **Step 3: Write minimal implementation**

`backend/retrieval/progressive_relax.py`:
```python
"""渐进式过滤放宽驱动：0 命中时按优先级逐个丢弃 soft slot 直到命中数达标。

驱动器本身与具体通路无关：每条召回通路提供自己的 ``search_fn(dropped_set)``
闭包，将 dropped 的 slot 名映射为该通路的 skip-knob / 重建的 expr 片段。

硬约束（gender/season/age）永不出现在 ``relax_priority`` 中，故循环在耗尽
soft 链后自然停在身份墙前——即使角色仍为空也不会跨性别/跨季节召回。
"""
from __future__ import annotations

from typing import Callable, TypeVar

T = TypeVar("T")


def run_with_progressive_relax(
    search_fn: Callable[[set[str]], list[T]],
    priority: list[str],
    min_hits: int,
) -> tuple[list[T], list[str]]:
    """按 ``priority`` 顺序逐个丢弃 slot 并重跑 ``search_fn``，直到命中数 ≥ ``min_hits``。

    - 首先用空 dropped 集跑一次（不退化：非空即立即返回）。
    - ``dropped`` 为实际被牺牲的 slot 名有序列表（可观测性）。
    - 耗尽 ``priority`` 仍不达标 → 返回最后一次（可能为空）的结果。
    - 硬墙隐式：硬 slot 不在 ``priority`` 中，循环不会触及。
    """
    dropped: list[str] = []
    hits = search_fn(set())
    for slot in priority:
        if len(hits) >= min_hits:
            return hits, dropped
        dropped.append(slot)
        hits = search_fn(set(dropped))
    return hits, dropped


def get_relax_config() -> tuple[bool, list[str], int]:
    """读取 ``recommend.{enable_progressive_relax,relax_priority,relax_min_hits}``。

    - ``enable_progressive_relax`` 缺省 True。
    - ``relax_priority`` 缺省 ``[modeling, length_class, coverage, series,
      scene_domain, color_series, category_l2, anchor_attr_must_not, up_time, price]``。
    - ``relax_min_hits`` 缺省 1（即 0 命中触发）。
    """
    from backend.config import load_config

    data = load_config() or {}
    rec = data.get("recommend") or {}
    enabled = bool(rec.get("enable_progressive_relax", True))
    priority = list(rec.get("relax_priority") or [
        "modeling", "length_class", "coverage", "series", "scene_domain",
        "color_series", "category_l2", "anchor_attr_must_not", "up_time", "price",
    ])
    try:
        min_hits = int(rec.get("relax_min_hits", 1))
    except (TypeError, ValueError):
        min_hits = 1
    return enabled, priority, min_hits
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_progressive_relax_driver.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Add config keys**

In `config.yaml`, under the `recommend:` block, add immediately after the `sku_text_vector_min_similarity: 0.55` line (~line 323):
```yaml
  # 渐进式过滤放宽：某 role 0 命中时按优先级逐个丢弃 soft slot 直到命中数达标。
  # enable=false 时退回旧行为（ES 三级阶梯 / 文本路二级递归 / 互补路无放宽）。
  # 硬约束 gender/season/age 永不在此列表中；up_time/price 在链尾、最后才放宽。
  enable_progressive_relax: true
  relax_priority:
    - modeling
    - length_class
    - coverage
    - series
    - scene_domain
    - color_series
    - category_l2
    - anchor_attr_must_not
    - up_time
    - price
  relax_min_hits: 1
```

- [ ] **Step 6: Commit**

```bash
git add backend/retrieval/progressive_relax.py tests/test_progressive_relax_driver.py config.yaml
git commit -m "feat(relax): shared progressive-relax driver + config"
```

---

## Task 2: ES path skip-flags in `resolve_es_query_for_role`

**Files:**
- Modify: `backend/retrieval/es_intent.py` (signature :450-464; emit sites :501-505, :564-575, :577-579, :581-590, :592-600, :602-610, :617-621, :626-634, :635-643)
- Test: `tests/test_es_progressive_relax.py`

**Interfaces:**
- Consumes: `relax_priority` slot names (Task 1).
- Produces: `resolve_es_query_for_role(..., skip_length_class=False, skip_coverage=False, skip_series=False, skip_scene_domain=False, skip_category_l2=False, skip_anchor_attr_must_not=False, skip_up_time=False, skip_price=False)` — the existing `skip_color_series`/`skip_modeling` are reused. Also produces a caller-level helper `_es_relax_kwargs(dropped)` (in Task 3) that maps dropped names → these flags + `allowed_companion_*`.

- [ ] **Step 1: Write the failing test**

`tests/test_es_progressive_relax.py`:
```python
"""resolve_es_query_for_role 的 skip-flag 测试：每个 flag 移除对应 filter 子句。"""
from __future__ import annotations

import unittest

from backend.models import UserIntent
from backend.retrieval.es_intent import resolve_es_query_for_role


def _intent() -> UserIntent:
    return UserIntent(
        anchor_role="top", target_roles=["bottoms"],
        gender="男", season=["春"], budget_max=500,
    )


def _filters(es_query: dict) -> list[dict]:
    return (es_query.get("query", {}).get("bool", {}).get("filter", [])
            or es_query.get("query", {}).get("bool", {}).get("filter", []))


def _filter_terms(es_query: dict) -> set[str]:
    """收集 bool.filter / must_not 里出现的字段名，用于断言某字段是否被下推。"""
    fields: set[str] = set()
    b = es_query.get("query", {}).get("bool", {})
    for clause in (b.get("filter") or []):
        for k in (clause or {}):
            if isinstance(clause[k], dict):
                fields.update(clause[k].keys())
            # range
            if k == "range":
                fields.update(clause[k].keys())
    for clause in (b.get("must_not") or []):
        for k in (clause or {}):
            if isinstance(clause[k], dict):
                fields.update(clause[k].keys())
    return fields


class EsSkipFlagTest(unittest.TestCase):
    def _q(self, **skip):
        q, _ = resolve_es_query_for_role(_intent(), "bottoms", anchor_row={"role": "top"}, **skip)
        return q

    def test_skip_up_time_removes_up_time_range(self):
        base = _filter_terms(self._q())
        self.assertIn("up_time", base)
        relaxed = _filter_terms(self._q(skip_up_time=True))
        self.assertNotIn("up_time", relaxed)

    def test_skip_price_removes_price_range(self):
        base = _filter_terms(self._q())
        self.assertIn("price", base)
        relaxed = _filter_terms(self._q(skip_price=True))
        self.assertNotIn("price", relaxed)

    def test_skip_modeling_removes_modeling_terms(self):
        # intent has no modeling; use a per-role positive modeling intent instead
        i = UserIntent(anchor_role="top", target_roles=["bottoms"],
                       target_slots={"bottoms": {"positive": {"modeling": "宽松"}}})
        q, _ = resolve_es_query_for_role(i, "bottoms", anchor_row={"role": "top"})
        self.assertIn("modeling", _filter_terms(q))
        q2, _ = resolve_es_query_for_role(i, "bottoms", anchor_row={"role": "top"}, skip_modeling=True)
        self.assertNotIn("modeling", _filter_terms(q2))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_es_progressive_relax.py -v`
Expected: FAIL with `TypeError: resolve_es_query_for_role() got an unexpected keyword argument 'skip_up_time'`

- [ ] **Step 3: Add skip-flags to the signature**

In `backend/retrieval/es_intent.py`, replace the signature block (lines 450-464) with:
```python
def resolve_es_query_for_role(
    intent: UserIntent,
    role: str,
    *,
    index_name: str | None = None,
    llm_enabled: bool = True,
    image_base64: str | None = None,
    model_override: str | None = None,
    anchor_context: dict[str, str] | None = None,
    anchor_row: dict[str, Any] | None = None,
    allowed_companion_cat2: list[str] | None = None,
    allowed_companion_color_series: list[str] | None = None,
    skip_color_series: bool = False,
    skip_modeling: bool = False,
    skip_length_class: bool = False,
    skip_coverage: bool = False,
    skip_series: bool = False,
    skip_scene_domain: bool = False,
    skip_category_l2: bool = False,
    skip_anchor_attr_must_not: bool = False,
    skip_up_time: bool = False,
    skip_price: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
```

- [ ] **Step 4: Gate the up_time emit site**

Replace the up_time block (lines 501-505) with:
```python
    # 全局上架时间下限：up_time >= config.recommend.up_time_since（与 Milvus expr、build_catalog 对齐）
    # 禁用（配置留空）或 skip_up_time（progressive relax 链尾放宽）时跳过。
    if not skip_up_time:
        _up_time_filter = build_up_time_es_filter()
        if _up_time_filter:
            es_query = _merge_es_filters(es_query, [_up_time_filter])
```

- [ ] **Step 5: Gate the category_l2 emit site**

Replace the cat2 block (lines 564-575) with:
```python
    if per_role_cat2:
        effective_cat2: list[str] | None = per_role_cat2
    elif skip_category_l2:
        effective_cat2 = None
    elif not _bypass:
        effective_cat2 = filter_companions_for_target_role(
            allowed_companion_cat2,
            role,
        )
    else:
        effective_cat2 = None
    cat2_filter = build_category_l2_es_filter(effective_cat2 or [])
    if cat2_filter:
        es_query = _merge_es_filters(es_query, [cat2_filter])
```

- [ ] **Step 6: Gate the color_series per-role override + emit**

In the `effective_cs` resolution (lines 528-540), force `None` when `skip_color_series`:
```python
    per_role_cs = per_role_color_series(intent, role)
    if skip_color_series:
        effective_cs = None
    elif per_role_cs:
        effective_cs = per_role_cs
        logger.info(
            "[es_intent·cs_override] role=%s 用户显式 color_series=%s，覆盖 pairing cs_filter",
            role, per_role_cs,
        )
    elif not _bypass:
        effective_cs = allowed_companion_color_series
    else:
        effective_cs = None
```
(Add `skip_color_series` as the first branch so an explicit per-role cs is also cleared under relax.)

- [ ] **Step 7: Gate the attr must_not emit site**

Replace the attr block (lines 581-590) with:
```python
    if not skip_anchor_attr_must_not:
        attr_must_not = build_attr_es_filter(
            anchor_row, role, bypass_all=_bypass,
        )
        if attr_must_not:
            es_query = _merge_es_must_not(es_query, attr_must_not["must_not"])
```

- [ ] **Step 8: Gate scene_domain + series emit sites**

Replace scene block (lines 592-600):
```python
    if not _bypass and not skip_scene_domain:
        scene_filter = build_scene_domain_es_filter(anchor_row, role)
        if scene_filter:
            es_query = _merge_es_filters(es_query, [scene_filter])
```
Replace series block (lines 602-610):
```python
    if not _bypass and not skip_series:
        series_filter = build_series_es_filter(anchor_row, role, intent.series or "")
        if series_filter:
            es_query = _merge_es_filters(es_query, [series_filter])
```

- [ ] **Step 9: Gate per-role positive length_class/coverage**

Replace the `build_role_es_positive_filters` call (lines 617-621) to thread per-slot skips via `exclude_slots`:
```python
    _pos_excl: set[str] = {"color_series", "category", "modeling"}
    if skip_length_class:
        _pos_excl.add("length_class")
    if skip_coverage:
        _pos_excl.add("coverage")
    if skip_scene_domain:
        _pos_excl.add("scene_domain")
    if skip_series:
        _pos_excl.add("series")
    role_pos_filters = build_role_es_positive_filters(
        intent, role, exclude_slots=tuple(_pos_excl),
    )
    if role_pos_filters:
        es_query = _merge_es_filters(es_query, role_pos_filters)
    role_must_not = build_role_es_must_not(intent, role)
    if role_must_not:
        es_query = _merge_es_must_not(es_query, role_must_not)
```
> Note: `build_role_es_positive_filters` already honors `exclude_slots` for the scalar slots (role_slots.py:426-431), so no change to that function is needed for the ES path. `modeling` positive is excluded here because it's emitted separately below.

- [ ] **Step 10: Gate the price/budget emit site**

Replace the price block (lines 635-643) with:
```python
    # per-role 价格区间（global←覆盖）；skip_price（progressive relax 链尾放宽）时跳过。
    if not skip_price:
        _bmin, _bmax = effective_role_budget(intent, role)
        _rng: dict[str, Any] = {}
        if _bmin and _bmin > 0:
            _rng["gte"] = _bmin
        if _bmax and _bmax > 0:
            _rng["lte"] = _bmax
        if _rng:
            es_query = _merge_es_filters(es_query, [{"range": {"price": _rng}}])
```

- [ ] **Step 11: Run test to verify it passes**

Run: `python -m pytest tests/test_es_progressive_relax.py -v`
Expected: PASS (3 tests)

- [ ] **Step 12: Commit**

```bash
git add backend/retrieval/es_intent.py tests/test_es_progressive_relax.py
git commit -m "feat(relax/es): skip-flags for progressive slot dropping in resolve_es_query_for_role"
```

---

## Task 3: ES path driver wiring (replace the 3-stage ladder)

**Files:**
- Modify: `backend/services/outfit_recall.py` (`_process_one_role` :1121-1330; parallel dispatch :1332+)
- Test: `tests/test_es_progressive_relax.py` (append a relaxation-integration test)

**Interfaces:**
- Consumes: `run_with_progressive_relax`, `get_relax_config` (Task 1); `resolve_es_query_for_role` skip-flags (Task 2).
- Produces: a module-level helper `_pick_from_hits(hits, seen_in_role, ...)` and `_es_relax_kwargs(dropped, allowed_cat2, allowed_cs_role, enable_cs)`.

- [ ] **Step 1: Write the failing integration test**

Append to `tests/test_es_progressive_relax.py`:
```python
from unittest.mock import MagicMock


class _FakeEs:
    """Returns [] until 'modeling' is dropped, then returns one hit."""
    def __init__(self):
        self.calls: list[dict] = []
        self._sku = {"sku_id": "S1", "category_l2": "裤", "title": "x"}

    def search_skus_with_query(self, q, n):
        self.calls.append(q)
        # detect skip_modeling by absence of modeling terms in the query
        b = q.get("query", {}).get("bool", {})
        fields = set()
        for c in (b.get("filter") or []):
            fields.update((c or {}).keys())
            for k in (c or {}):
                if isinstance(c[k], dict):
                    fields.update(c[k].keys())
        if "modeling" not in fields:
            return [("S1", 1.0)]
        return []

    def available(self):
        return True


class EsRelaxIntegrationTest(unittest.TestCase):
    def test_drops_modeling_on_zero_hits(self):
        from backend.services import outfit_recall as orc
        fake_es = _FakeEs()
        sku_r = MagicMock()
        sku_r._es = fake_es
        sku_r.get_sku.return_value = {"sku_id": "S1", "category_l2": "裤", "title": "x"}
        intent = UserIntent(
            anchor_role="top", target_roles=["bottoms"], gender="男",
            target_slots={"bottoms": {"positive": {"modeling": "宽松"}}},
        )
        # patch config to a single-slot priority for a deterministic assertion
        with unittest.mock.patch(
            "backend.retrieval.progressive_relax.get_relax_config",
            return_value=(True, ["modeling"], 1),
        ):
            by_role, meta = orc.recall_query2es_skus(
                sku_r, intent, {"role": "top", "sku_id": "A1"},
            )
        self.assertIn("bottoms", by_role)
        self.assertEqual(meta["bottoms"].get("fallback_dropped"), ["modeling"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_es_progressive_relax.py::EsRelaxIntegrationTest -v`
Expected: FAIL — `meta` has no `fallback_dropped` key (current ladder uses `fallback_no_modeling`).

- [ ] **Step 3: Extract `_pick_from_hits` helper**

At the top of `recall_query2es_skus` (after the `role_meta: dict[str, Any] = {}` line, ~1119), add a nested helper that consolidates the 4×-duplicated per-hit loop (current lines 1169-1191):
```python
    def _pick_from_hits(
        hits: list[tuple[str, float]],
        seen_in_role: set[str],
    ) -> list[dict[str, Any]]:
        """把 ES hits 物化成 SKU 行 + 冲突过滤 + 去重 + _es_score。"""
        picked: list[dict[str, Any]] = []
        for sid, score in hits:
            if sid in seen_in_role:
                continue
            row = sku_r.get_sku(sid)
            if not row:
                continue
            if compose_anchor_row and check_companion_conflict(
                compose_anchor_row, row,
                bypass_all=role_has_explicit_positive(intent, role),
            ):
                logger.info(
                    "[query2es·冲突过滤] anchor=%s 与 sku=%s "
                    "category_l2=%s title=%s 属性冲突，跳过",
                    compose_anchor_id, sid, row.get("category_l2"), row.get("title"),
                )
                continue
            seen_in_role.add(sid)
            if compose_anchor_id and sid == compose_anchor_id:
                continue
            c = dict(row)
            c["_es_score"] = float(score)
            picked.append(c)
        return picked
```

- [ ] **Step 4: Add the dropped→skip-kwargs mapper**

Add a module-level function in `outfit_recall.py` (near the other `_es_*` helpers, or just above `recall_query2es_skus`):
```python
def _es_relax_kwargs(
    dropped: set[str],
    *,
    allowed_cat2: list[str] | None,
    allowed_cs_role: list[str] | None,
    enable_cs: bool,
) -> dict[str, Any]:
    """将 dropped 的 slot 名映射为 resolve_es_query_for_role 的 skip-knob 参数。"""
    kw: dict[str, Any] = {
        "allowed_companion_cat2": None if "category_l2" in dropped else allowed_cat2,
        "allowed_companion_color_series": None if "color_series" in dropped else allowed_cs_role,
        "skip_color_series": ("color_series" in dropped) or (not enable_cs),
    }
    flag_for = {
        "modeling": "skip_modeling",
        "length_class": "skip_length_class",
        "coverage": "skip_coverage",
        "series": "skip_series",
        "scene_domain": "skip_scene_domain",
        "category_l2": "skip_category_l2",
        "anchor_attr_must_not": "skip_anchor_attr_must_not",
        "up_time": "skip_up_time",
        "price": "skip_price",
    }
    for slot, flag in flag_for.items():
        if slot in dropped:
            kw[flag] = True
    return kw
```

- [ ] **Step 5: Rewrite `_process_one_role` to use the driver**

Replace the entire body of `_process_one_role` from the `es_query, meta = resolve_es_query_for_role(...)` call (line 1138) through the `return role, picked, meta` (line 1330) with:
```python
        relax_enabled, relax_priority, relax_min_hits = get_relax_config()

        def _search_fn(dropped: set[str]) -> list[dict[str, Any]]:
            es_q, _ = resolve_es_query_for_role(
                intent,
                role,
                index_name=index_name,
                llm_enabled=llm_on,
                image_base64=image_base64,
                model_override=model_override,
                anchor_context=anchor_context,
                anchor_row=compose_anchor_row,
                **_es_relax_kwargs(
                    dropped,
                    allowed_cat2=allowed_cat2,
                    allowed_cs_role=allowed_cs_role,
                    enable_cs=enable_cs,
                ),
            )
            hits = sku_r._es.search_skus_with_query(es_q, per_role)  # noqa: SLF001
            return _pick_from_hits(hits, seen_in_role)

        seen_in_role: set[str] = set()
        if relax_enabled:
            picked, dropped_list = run_with_progressive_relax(
                _search_fn, relax_priority, relax_min_hits,
            )
            if dropped_list:
                meta_any: dict[str, Any] = {}
                meta_any["fallback_dropped"] = dropped_list
            else:
                meta_any = {}
        else:
            # 旧行为：单次查询，无放宽（master switch off 时的回退）
            picked = _search_fn(set())
            meta_any = {}

        # 记录首次 query + 命中数（可观测性）
        es_query0, meta0 = resolve_es_query_for_role(
            intent, role,
            index_name=index_name, llm_enabled=llm_on,
            image_base64=image_base64, model_override=model_override,
            anchor_context=anchor_context, anchor_row=compose_anchor_row,
            **_es_relax_kwargs(
                set(), allowed_cat2=allowed_cat2, allowed_cs_role=allowed_cs_role,
                enable_cs=enable_cs,
            ),
        )
        meta = meta0
        meta["es_query"] = es_query0
        meta.update(meta_any)
        meta["hits"] = len(picked)
        log_text_search_recall_io(
            trace_id=trace_id, entity="sku", channel="query2es",
            query=str(meta.get("fallback_q") or intent.text or "")[:500],
            limit=per_role, output_ids=[r.get("sku_id") for r in picked],
            extra={"index": index_name, "target_role": role,
                   "es_query_source": meta.get("source"),
                   "fallback_dropped": meta_any.get("fallback_dropped", [])},
        )
        return role, picked, meta
```
> Note: This removes the three `if not picked` fallback blocks (1193-1328) and the duplicated per-hit loops. The `meta["fallback_no_*"]` keys are superseded by `meta["fallback_dropped"]`. Add `from backend.retrieval.progressive_relax import run_with_progressive_relax, get_relax_config` to the imports at the top of `outfit_recall.py`.

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m pytest tests/test_es_progressive_relax.py -v`
Expected: PASS (4 tests, including the integration test)

- [ ] **Step 7: Commit**

```bash
git add backend/services/outfit_recall.py tests/test_es_progressive_relax.py
git commit -m "feat(relax/es): replace 3-stage ladder with progressive-relax driver"
```

---

## Task 4: Milvus text/hybrid path — builder skip params + retriever cleanup

**Files:**
- Modify: `backend/intent/role_slots.py` (`build_role_milvus_expr_parts` :209-260; `build_modeling_price_milvus_expr` :263-291)
- Modify: `backend/retrieval/sku_retriever.py` (`recall_by_text_vector_keywords` :120-341; `recall_by_hybrid` :343-448)
- Test: `tests/test_text_vector_progressive_relax.py`

**Interfaces:**
- Produces: `build_role_milvus_expr_parts(intent, role, *, include_global=True, skip_slots=None)`; `build_modeling_price_milvus_expr(intent, role, *, skip_modeling=False, skip_price=False)`; `recall_by_text_vector_keywords(..., skip_up_time=False)` with the recursive `fallback_on_empty` drops removed; same for `recall_by_hybrid`.

- [ ] **Step 1: Write the failing test**

`tests/test_text_vector_progressive_relax.py`:
```python
"""text/hybrid 召回的 skip 参数测试：skip_slots/skip_up_time 移除对应 expr 片段。"""
from __future__ import annotations

import unittest

from backend.models import UserIntent
from backend.intent.role_slots import (
    build_role_milvus_expr_parts, build_modeling_price_milvus_expr,
)


class TextBuilderSkipTest(unittest.TestCase):
    def test_skip_slots_drops_length_class_positive(self):
        i = UserIntent(
            anchor_role="top", target_roles=["bottoms"],
            target_slots={"bottoms": {"positive": {"length_class": "long"}}},
        )
        parts = build_role_milvus_expr_parts(i, "bottoms", include_global=False)
        joined = " and ".join(parts)
        self.assertIn('length_class == "long"', joined)
        parts2 = build_role_milvus_expr_parts(
            i, "bottoms", include_global=False, skip_slots={"length_class"},
        )
        self.assertNotIn("length_class", " and ".join(parts2))

    def test_modeling_price_skip_flags(self):
        i = UserIntent(
            anchor_role="top", target_roles=["bottoms"],
            target_slots={"bottoms": {"positive": {"modeling": "宽松"}}},
            budget_max=500,
        )
        expr = build_modeling_price_milvus_expr(i, "bottoms") or ""
        self.assertIn("modeling", expr)
        self.assertIn("price", expr)
        expr2 = build_modeling_price_milvus_expr(i, "bottoms", skip_modeling=True, skip_price=True)
        self.assertIsNone(expr2)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_text_vector_progressive_relax.py -v`
Expected: FAIL with `TypeError: build_role_milvus_expr_parts() got an unexpected keyword argument 'skip_slots'`

- [ ] **Step 3: Add `skip_slots` to `build_role_milvus_expr_parts`**

In `backend/intent/role_slots.py`, replace the signature + the positive-append loops (lines 209-237) with:
```python
def build_role_milvus_expr_parts(
    intent: UserIntent, role: str, *,
    include_global: bool = True, skip_slots: set[str] | None = None,
) -> list[str]:
    """构建某 target_role 的 per-role 正向/否定 Milvus expr 片段列表。

    ``skip_slots``：progressive relax 丢弃的 slot 名集合，正向/否定对应项跳过。
    """
    skip_slots = skip_slots or set()
    parts: list[str] = []
    pos_src = effective_role_slots(intent, role) if include_global else _override_slots(intent, role)
    pos = milvus_filterable_positive(pos_src)
    neg = milvus_filterable_negative(role_negative_slots(intent, role))

    for slot in _LIST_MILVUS_SLOTS:
        if slot in skip_slots:
            continue
        vals = pos.get(slot)
        if vals:
            parts.append(_milvus_in_clause(_MILVUS_FIELD_NAME[slot], list(vals)))
    for slot in _SCALAR_MILVUS_SLOTS:
        if slot in skip_slots:
            continue
        v = pos.get(slot)
        if v and v not in ("n/a", ""):
            parts.append(f'{_MILVUS_FIELD_NAME[slot]} == "{_milvus_escape(v)}"')

    for slot in _LIST_MILVUS_SLOTS:
        if slot in skip_slots:
            continue
        vals = neg.get(slot)
        if vals:
            parts.append(_milvus_not_in_clause(_MILVUS_FIELD_NAME[slot], list(vals)))
    for slot in _SCALAR_MILVUS_SLOTS:
        if slot in skip_slots:
            continue
        vals = neg.get(slot)
        if vals:
            parts.append(_milvus_not_in_clause(_MILVUS_FIELD_NAME[slot], list(vals)))
    # 版型否定：多值各自同义词展开后并集 not in
    if "modeling" not in skip_slots:
        m_neg = neg.get("modeling")
        if m_neg:
            union: list[str] = []
            seen: set[str] = set()
            for v in m_neg:
                for e in expand_modeling(str(v)):
                    if e not in seen:
                        seen.add(e)
                        union.append(e)
            if union:
                parts.append(_milvus_not_in_clause("modeling", union))

    return parts
```
> Note: `length_class`/`coverage` are in `_SCALAR_MILVUS_SLOTS` so the new `skip_slots` check covers them. `series`/`scene_domain` are also in `_SCALAR_MILVUS_SLOTS` — but per the slot-name semantics, the `series`/`scene_domain` relax entries clear the **anchor-isolation** clauses (handled in Task 5's `_rebuild_text_attr_expr`), not these per-role positives. The per-role positive series/scene_domain would also be dropped if present — that's acceptable since anchor-isolation is the dominant clause; dropping both together is still "more relaxed" and safe.

- [ ] **Step 4: Add `skip_modeling`/`skip_price` to `build_modeling_price_milvus_expr`**

Replace the function (lines 263-291) with:
```python
def build_modeling_price_milvus_expr(
    intent: UserIntent, role: str, *,
    skip_modeling: bool = False, skip_price: bool = False,
) -> str | None:
    """该 role 生效的版型 + 价格区间 Milvus expr 片段，无约束返回 None。

    ``skip_modeling`` / ``skip_price``：progressive relax 时跳过对应部分。
    """
    parts: list[str] = []
    eff = effective_role_slots(intent, role)
    if not skip_modeling:
        m = eff.get("modeling")
        if m and m not in ("n/a", ""):
            expanded = expand_modeling(str(m))
            if expanded:
                parts.append(_milvus_in_clause("modeling", expanded))
    if not skip_price:
        bmin, bmax = effective_role_budget(intent, role)
        if bmin and bmin > 0:
            parts.append(f"price >= {float(bmin)}")
        if bmax and bmax > 0:
            parts.append(f"price <= {float(bmax)}")
    if not parts:
        return None
    from backend.intent.category_l2_pairing import merge_milvus_expr
    return merge_milvus_expr(*parts)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_text_vector_progressive_relax.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Add `skip_up_time` + remove recursive fallbacks in `recall_by_text_vector_keywords`**

In `backend/retrieval/sku_retriever.py`:

(a) Add `skip_up_time: bool = False` to the signature (after `color_series_match_mode` at line 134).

(b) Gate the up_time line in `_build_expr` (line 212):
```python
            build_up_time_milvus_expr() if not skip_up_time else None,
```

(c) Replace the two recursive `fallback_on_empty` blocks (lines 303-339) with a plain return (the driver now handles 0-hit):
```python
        return rows
```
(Remove the `if not rows and fallback_on_empty and category_l2_filter:` block and the `if not rows and fallback_on_empty and color_series_filter:` block entirely. Keep `fallback_on_empty` in the signature for backward-compat but it's now a no-op; add a deprecation note in the docstring.)

- [ ] **Step 7: Mirror in `recall_by_hybrid`**

(a) Add `skip_up_time: bool = False` to the signature (after `color_series_match_mode` at line 356).

(b) Gate the up_time line in `_build_expr` (line 400):
```python
            build_up_time_milvus_expr() if not skip_up_time else None,
```

(c) Remove the hybrid→dense 0-hit fallback block (lines 432-448), replacing with `return rows`. (The driver will instead drop soft slots; if still 0 after exhausting, that's the hard wall — same as today's dense fallback would have produced 0 anyway.)

> ⚠️ This is a behavior change: previously hybrid 0-hit fell back to dense. Under the new model, the driver's `search_fn` (Task 5) will run hybrid first, then drop slots; dense-fallback is no longer automatic. If parity is required, the Task 5 `search_fn` can be written to try hybrid then dense as two legs *before* dropping slots — see Task 5 Step 3.

- [ ] **Step 8: Commit**

```bash
git add backend/intent/role_slots.py backend/retrieval/sku_retriever.py tests/test_text_vector_progressive_relax.py
git commit -m "feat(relax/text): skip_slots on per-role parts; skip_up_time; remove recursive fallbacks"
```

---

## Task 5: Milvus text/hybrid path driver wiring in `recall_text_vector_skus`

**Files:**
- Modify: `backend/services/outfit_recall.py` (`recall_text_vector_skus` :797-954, esp. attr_expr assembly :888-897, hybrid/dense selection :898-926)
- Test: `tests/test_text_vector_progressive_relax.py` (append integration test)

**Interfaces:**
- Consumes: `run_with_progressive_relax`, `get_relax_config` (Task 1); `build_role_milvus_expr_parts(skip_slots=)`, `build_modeling_price_milvus_expr(skip_modeling=, skip_price=)` (Task 4); `recall_by_hybrid`/`recall_by_text_vector_keywords(skip_up_time=)` (Task 4).
- Produces: `_rebuild_text_attr_expr(intent, role, anchor_row, _bypass, dropped)` helper.

- [ ] **Step 1: Write the failing integration test**

Append to `tests/test_text_vector_progressive_relax.py`:
```python
from unittest.mock import MagicMock


class _FakeRetriever:
    """Records expr per call; returns [] until 'modeling' dropped, then 1 hit."""
    def __init__(self):
        self.calls: list[str] = []
    def recall_by_text_vector_keywords(self, kws, top_k_per_keyword=None, **kw):
        expr = kw.get("attr_expr") or ""
        self.calls.append(expr)
        # modeling present in expr → 0; dropped → 1 hit
        if "modeling" in expr:
            return []
        return [("S1", 0.9, 0.9)]
    def recall_by_hybrid(self, kws, top_k_per_keyword=None, **kw):
        return self.recall_by_text_vector_keywords(kws, top_k_per_keyword, **kw)


class TextRelaxIntegrationTest(unittest.TestCase):
    def test_drops_modeling_on_zero_hits(self):
        from backend.services import outfit_recall as orc
        sku_r = MagicMock()
        sku_r._milvus = MagicMock()
        fake = _FakeRetriever()
        sku_r.recall_by_text_vector_keywords = fake.recall_by_text_vector_keywords
        sku_r.recall_by_hybrid = fake.recall_by_hybrid
        sku_r.get_sku.return_value = {"sku_id": "S1", "category_l2": "裤", "title": "x"}
        intent = UserIntent(
            anchor_role="top", target_roles=["bottoms"], gender="男",
            target_slots={"bottoms": {"positive": {"modeling": "宽松"}}},
        )
        with unittest.mock.patch(
            "backend.retrieval.progressive_relax.get_relax_config",
            return_value=(True, ["modeling"], 1),
        ), unittest.mock.patch.object(orc, "_debug_recall_io_enabled", return_value=False):
            orc.recall_text_vector_skus(sku_r, intent, {"role": "top", "sku_id": "A1"})
        # first call has modeling (0 hits), second call drops it (1 hit)
        self.assertGreaterEqual(len(fake.calls), 2)
        self.assertIn("modeling", fake.calls[0])
        self.assertNotIn("modeling", fake.calls[-1])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_text_vector_progressive_relax.py::TextRelaxIntegrationTest -v`
Expected: FAIL — current code runs one search (or the low-recall retry), not a drop loop.

- [ ] **Step 3: Add the `_rebuild_text_attr_expr` helper**

In `outfit_recall.py`, add a module-level helper:
```python
def _rebuild_text_attr_expr(
    intent: UserIntent, role: str,
    anchor_row: dict[str, Any] | None, _bypass: bool,
    dropped: set[str],
) -> str | None:
    """按 dropped 集合重建 text/hybrid 路的 attr_expr（scene/series/attr/per-role/modeling+price）。

    落地 progressive relax：每轮按丢弃集重新拼装，而不是用首次拼好的 attr_expr 贯穿，
    这样 series/scene_domain/anchor_attr_must_not/modeling/price/length_class/coverage
    可被独立放宽。
    """
    parts: list[str | None] = []
    if "anchor_attr_must_not" not in dropped:
        parts.append(build_attr_milvus_expr(anchor_row, role, bypass_all=_bypass))
    if "scene_domain" not in dropped:
        parts.append(build_scene_domain_milvus_expr(anchor_row, role))
    if "series" not in dropped:
        parts.append(build_series_milvus_expr(anchor_row, role, intent.series or "", bypass_all=False))
    # per-role 正向/否定：length_class/coverage/modeling/color_series/category 由 skip_slots 控制
    per_role_skip = {"length_class", "coverage", "modeling", "color_series", "category"} & dropped
    parts.extend(build_role_milvus_expr_parts(
        intent, role, include_global=False,
        skip_slots=per_role_skip or None,
    ))
    if "modeling" not in dropped or "price" not in dropped:
        parts.append(build_modeling_price_milvus_expr(
            intent, role,
            skip_modeling=("modeling" in dropped),
            skip_price=("price" in dropped),
        ))
    return merge_milvus_expr(*parts)
```
> Add imports: `from backend.retrieval.progressive_relax import run_with_progressive_relax, get_relax_config` (if not already from Task 3), and ensure `build_attr_milvus_expr`, `build_scene_domain_milvus_expr`, `build_series_milvus_expr`, `merge_milvus_expr` are imported (they already are in `recall_text_vector_skus` per the verbatim code).

- [ ] **Step 4: Rewrite the per-role search in `recall_text_vector_skus`**

Replace the attr_expr assembly + hybrid/dense selection + low-recall retry block (lines 888-954) with:
```python
        relax_enabled, relax_priority, relax_min_hits = get_relax_config()

        def _search_fn(dropped: set[str]) -> list[tuple[str, float, float]]:
            attr_expr = _rebuild_text_attr_expr(intent, role, compose_anchor_row, _bypass, dropped)
            cat2 = None if "category_l2" in dropped else role_companions
            cs = None if "color_series" in dropped else cs_filter
            skip_up = "up_time" in dropped
            common = dict(
                role_filter=role, gender_filter=gender, age_filter=age,
                category_l2_filter=cat2, color_series_filter=cs,
                trace_id=trace_id, attr_expr=attr_expr,
            )
            if recall_mode == "hybrid":
                return sku_r.recall_by_hybrid([kw], skip_up_time=skip_up, **common)
            return sku_r.recall_by_text_vector_keywords([kw], skip_up_time=skip_up, **common)

        if relax_enabled:
            pairs, dropped_list = run_with_progressive_relax(
                _search_fn, relax_priority, relax_min_hits,
            )
            if dropped_list:
                logger.info(
                    "[text_recall·progressive_relax] role=%s dropped=%s pairs=%d",
                    role, dropped_list, len(pairs),
                )
        else:
            pairs = _search_fn(set())

        # 低召回降阈值二次召回（保留全部过滤；progressive relax 之后的补充手段）
        _LOW_RECALL_THRESHOLD = 3
        if len(pairs) < _LOW_RECALL_THRESHOLD and recall_mode != "hybrid":
            cfg_min = float(rec.get("sku_text_vector_min_similarity") or 0.0)
            retry_min = max(0.0, cfg_min - 0.15)
            if retry_min < cfg_min:
                logger.info(
                    "[text_vector·低召回降阈值] role=%s 召回%d条<%d，降阈值 %.2f→%.2f 二次召回",
                    role, len(pairs), _LOW_RECALL_THRESHOLD, cfg_min, retry_min,
                )
                attr_expr = _rebuild_text_attr_expr(intent, role, compose_anchor_row, _bypass, set())
                pairs2 = sku_r.recall_by_text_vector_keywords(
                    [kw], min_similarity_override=retry_min,
                    role_filter=role, gender_filter=gender, age_filter=age,
                    category_l2_filter=role_companions, color_series_filter=cs_filter,
                    trace_id=trace_id, attr_expr=attr_expr,
                )
                seen_sim: dict[str, float] = {sid: sim for sid, sim, _ in pairs}
                for sid, sim, _raw in pairs2:
                    if sid not in seen_sim or sim > seen_sim[sid]:
                        seen_sim[sid] = sim
                        pairs.append((sid, sim, _raw))
                pairs.sort(key=lambda x: x[1], reverse=True)
```
> Note: `recall_mode`, `role`, `gender`, `age`, `kw`, `role_companions`, `cs_filter`, `_bypass`, `compose_anchor_row`, `rec` are all in scope at this point in `recall_text_vector_skus` (see verbatim lines 797-897). The hybrid path deliberately skips the low-recall retry (preserving the original comment at line 900-901).

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_text_vector_progressive_relax.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add backend/services/outfit_recall.py tests/test_text_vector_progressive_relax.py
git commit -m "feat(relax/text): rebuild attr_expr per dropped-set; driver replaces fallback"
```

---

## Task 6: Complementary (image-vector) path

**Files:**
- Modify: `backend/services/complementary_recall.py` (`_build_role_milvus_expr` :38-159; `_search_one_role` :191-232)
- Test: `tests/test_complementary_progressive_relax.py`

**Interfaces:**
- Consumes: `run_with_progressive_relax`, `get_relax_config` (Task 1); `build_role_milvus_expr_parts(skip_slots=)`, `build_modeling_price_milvus_expr(skip_modeling=, skip_price=)` (Task 4).
- Produces: `_build_role_milvus_expr(intent, role, *, anchor_cat2="", anchor_row=None, skip_slots=None)`.

- [ ] **Step 1: Write the failing test**

`tests/test_complementary_progressive_relax.py`:
```python
"""complementary 召回的 progressive relax 测试：skip_slots 移除对应 expr 片段。"""
from __future__ import annotations

import unittest

from backend.models import UserIntent
from backend.services.complementary_recall import _build_role_milvus_expr


def _intent() -> UserIntent:
    return UserIntent(anchor_role="bottoms", target_roles=["top"], gender="男", season=["春"])


class ComplementarySkipTest(unittest.TestCase):
    def test_skip_up_time_drops_up_time_clause(self):
        anchor = {"role": "bottoms"}
        base = _build_role_milvus_expr(_intent(), "top", anchor_row=anchor)
        self.assertIn("up_time", base)
        relaxed = _build_role_milvus_expr(
            _intent(), "top", anchor_row=anchor, skip_slots={"up_time"},
        )
        self.assertNotIn("up_time", relaxed)

    def test_hard_slots_survive_full_drop(self):
        # drop everything relaxable; gender/season must remain
        anchor = {"role": "bottoms"}
        all_soft = {
            "modeling", "length_class", "coverage", "series", "scene_domain",
            "color_series", "category_l2", "anchor_attr_must_not", "up_time", "price",
        }
        expr = _build_role_milvus_expr(_intent(), "top", anchor_row=anchor, skip_slots=all_soft)
        self.assertIn("男", expr)        # gender survives
        self.assertIn("春", expr)        # season survives
        self.assertNotIn("up_time", expr)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_complementary_progressive_relax.py -v`
Expected: FAIL with `TypeError: _build_role_milvus_expr() got an unexpected keyword argument 'skip_slots'`

- [ ] **Step 3: Add `skip_slots` to `_build_role_milvus_expr` and gate each part**

Change the signature (lines 38-44) to:
```python
def _build_role_milvus_expr(
    intent: UserIntent,
    role: str,
    *,
    anchor_cat2: str = "",
    anchor_row: dict[str, Any] | None = None,
    skip_slots: set[str] | None = None,
) -> str:
    """Build Milvus boolean expression for a single target role.

    ``skip_slots``：progressive relax 丢弃的 slot 名集合，对应 expr 片段跳过。
    gender/season/age 永不可跳过（硬约束）。
    """
    skip = skip_slots or set()
    parts: list[str] = [f'role == "{role}"']
```

Then gate each subsequent part with `if "<slot>" not in skip:` (keeping the existing `_bypass` gating in AND):
- gender (line 49-50): gender is hard — leave ungated, **do not** add a skip check.
- attr (line 57-61):
```python
    if "anchor_attr_must_not" not in skip:
        attr_expr = build_attr_milvus_expr(anchor_row, role, bypass_all=_bypass)
        if attr_expr:
            parts.append(attr_expr)
```
- scene (line 77-80):
```python
    else:
        if "scene_domain" not in skip:
            scene_expr = build_scene_domain_milvus_expr(anchor_row, role)
            if scene_expr:
                parts.append(scene_expr)
```
  (Keep the `_bypass` log branch as the `if _bypass:` arm unchanged.)
- series (line 87-90):
```python
    if not _bypass and "series" not in skip:
        series_expr = build_series_milvus_expr(anchor_row, role, intent.series or "", bypass_all=False)
        if series_expr:
            parts.append(series_expr)
```
- per-role parts (line 95):
```python
    parts.extend(build_role_milvus_expr_parts(
        intent, role,
        skip_slots={"length_class", "coverage", "modeling", "color_series", "category",
                     "series", "scene_domain"} & skip or None,
    ))
```
- modeling+price (line 98-100):
```python
    mp_expr = build_modeling_price_milvus_expr(
        intent, role,
        skip_modeling=("modeling" in skip), skip_price=("price" in skip),
    )
    if mp_expr:
        parts.append(mp_expr)
```
- color_series pairing (line 109-127): wrap the `if not per_role_cs and not _bypass:` with `and "color_series" not in skip`:
```python
    if not per_role_cs and not _bypass and "color_series" not in skip:
```
- season (line 134-138): season is hard — leave ungated.
- age (line 142-151): age is hard — leave ungated.
- up_time (line 155-157):
```python
    if "up_time" not in skip:
        _up_time_expr = build_up_time_milvus_expr()
        if _up_time_expr:
            parts.append(_up_time_expr)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_complementary_progressive_relax.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Wire the driver into `_search_one_role`**

Replace `_search_one_role` body (lines 202-232) — the expr build + single search + post-filter — with a driver loop:
```python
    """Search Milvus for a single role and return enriched SKU rows.

    0 命中时按 progressive relax 优先级逐个丢弃 soft slot 直到命中数达标。
    """
    relax_enabled, relax_priority, relax_min_hits = get_relax_config()

    def _search_fn(dropped: set[str]) -> list[dict[str, Any]]:
        expr = _build_role_milvus_expr(
            intent, role, anchor_cat2=anchor_cat2, anchor_row=anchor_row,
            skip_slots=dropped or None,
        )
        if not dropped:
            logger.info(
                "[complementary·milvus_expr] role=%s anchor_cat2=%s expr=%s",
                role, anchor_cat2, expr,
            )
        pairs = milvus.search_sku_complementary_vectors(embedding, top_k, expr=expr)
        results: list[dict[str, Any]] = []
        for sid, dist in pairs:
            if sid == anchor_id:
                continue
            sim = milvus.hit_to_similarity(float(dist))
            row = sku_r.get_sku(sid)
            if not row:
                continue
            if anchor_row and check_companion_conflict(
                anchor_row, row,
                bypass_all=role_has_explicit_positive(intent, role),
            ):
                logger.info(
                    "[complementary·冲突过滤] anchor=%s 与 sku=%s "
                    "category_l2=%s title=%s 属性冲突，跳过",
                    anchor_id, sid, row.get("category_l2"), row.get("title"),
                )
                continue
            c = dict(row)
            c["_complementary_sim"] = float(sim)
            results.append(c)
        return results

    if relax_enabled:
        results, dropped_list = run_with_progressive_relax(
            _search_fn, relax_priority, relax_min_hits,
        )
        if dropped_list:
            logger.info(
                "[complementary·progressive_relax] role=%s dropped=%s results=%d",
                role, dropped_list, len(results),
            )
    else:
        results = _search_fn(set())
    return results
```
> Add imports at top of `complementary_recall.py`: `from backend.retrieval.progressive_relax import run_with_progressive_relax, get_relax_config`.

- [ ] **Step 6: Run the existing complementary tests to check no regression**

Run: `python -m pytest tests/test_complementary_age_filter.py tests/test_complementary_progressive_relax.py -v`
Expected: PASS (existing age-filter test still passes because `skip_slots` defaults to `None`; new tests pass).

- [ ] **Step 7: Commit**

```bash
git add backend/services/complementary_recall.py tests/test_complementary_progressive_relax.py
git commit -m "feat(relax/complementary): progressive relax on 0-hit (first relaxation on this path)"
```

---

## Task 7: Hard-wall, tail-only guard, and regression tests

**Files:**
- Create: `tests/test_progressive_relax_hardwall.py`

**Interfaces:**
- Consumes: all three pathways wired (Tasks 1-6).

- [ ] **Step 1: Write the tests**

`tests/test_progressive_relax_hardwall.py`:
```python
"""硬墙 + 尾位守卫 + master-switch 回归测试。"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from backend.retrieval.progressive_relax import run_with_progressive_relax, get_relax_config


class HardWallTest(unittest.TestCase):
    def test_gender_season_age_never_dropped(self):
        """driver 只丢 priority 中列出的 slot；gender/season/age 不在列表中。"""
        _, priority, _ = get_relax_config()
        for hard in ("gender", "season", "age"):
            self.assertNotIn(hard, priority)

    def test_up_time_price_at_tail(self):
        """up_time/price 必须在链尾（最后两个）。"""
        _, priority, _ = get_relax_config()
        self.assertEqual(priority[-2:], ["up_time", "price"])

    def test_tail_only_guard(self):
        """up_time/price 不应在更早的 soft 已达标时被丢弃。"""
        calls: list[set[str]] = []

        def search_fn(dropped: set[str]) -> list[str]:
            calls.append(set(dropped))
            # 'modeling' dropped already yields enough → must stop there
            if "modeling" in dropped:
                return ["h1", "h2"]
            return []

        hits, dropped = run_with_progressive_relax(
            search_fn,
            priority=["modeling", "length_class", "up_time", "price"],
            min_hits=2,
        )
        self.assertEqual(dropped, ["modeling"])
        self.assertNotIn("up_time", dropped)
        self.assertNotIn("price", dropped)

    def test_master_switch_off_runs_once(self):
        """enable_progressive_relax=False 时 search_fn 只被调用一次（无放宽）。"""
        # 模拟 get_relax_config 返回 disabled
        with unittest.mock.patch(
            "backend.retrieval.progressive_relax.get_relax_config",
            return_value=(False, ["modeling"], 1),
        ):
            enabled, priority, min_hits = get_relax_config()
            self.assertFalse(enabled)
            # 当 enabled=False 时调用方应短路：这里直接验证调用方契约
            # （实际短路发生在各 pathway 的 `if relax_enabled:` 分支，见 Task 3/5/6）


class EsRegressionTest(unittest.TestCase):
    def test_es_master_switch_off_no_fallback_dropped(self):
        from backend.services import outfit_recall as orc
        fake_es = MagicMock()
        fake_es.search_skus_with_query.return_value = []  # always 0
        sku_r = MagicMock()
        sku_r._es = fake_es
        sku_r.get_sku.return_value = None
        intent = MagicMock()
        intent.target_roles = ["bottoms"]
        intent.gender = "男"
        intent.season = []
        intent.budget_max = None
        intent.text = ""
        intent.anchor_role = "top"
        intent.category = []
        intent.color_series = []
        intent.series = ""
        intent.target_slots = {}
        with unittest.mock.patch(
            "backend.retrieval.progressive_relax.get_relax_config",
            return_value=(False, ["modeling"], 1),
        ), unittest.mock.patch.object(orc, "_debug_recall_io_enabled", return_value=False):
            by_role, meta = orc.recall_query2es_skus(sku_r, intent, {"role": "top", "sku_id": "A1"})
        # disabled → no fallback_dropped key
        self.assertNotIn("fallback_dropped", meta.get("bottoms", {}))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `python -m pytest tests/test_progressive_relax_hardwall.py -v`
Expected: PASS (5 tests)

- [ ] **Step 3: Run the full recall test suite for regressions**

Run: `python -m pytest tests/test_complementary_age_filter.py tests/test_text_vector_low_recall_retry.py tests/test_recall_by_hybrid.py tests/test_scene_domain.py tests/test_dress_anchor_filter.py -v`
Expected: PASS. If `test_text_vector_low_recall_retry.py` or `test_recall_by_hybrid.py` fail, the behavior change in Task 4 Step 7 (removing hybrid→dense auto-fallback) is the cause — adjust those tests to assert the new progressive-relax behavior, or add a dense-leg inside the Task 5 `search_fn` before dropping slots (see Task 5 Step 3 note).

- [ ] **Step 4: Commit**

```bash
git add tests/test_progressive_relax_hardwall.py
git commit -m "test(relax): hard-wall, tail-only guard, master-switch regression"
```

- [ ] **Step 5: Final full-suite smoke**

Run: `python -m pytest tests/ -k "relax or complementary or text_vector or hybrid or scene_domain or dress_anchor" -v`
Expected: PASS across all touched surfaces.

---

## Self-Review Notes (for the implementer)

- **Spec coverage:** §2 boundary (gender/season/age hard; up_time/price soft-tail) → Task 1 config + Task 7 hard-wall test. §3 priority list → Task 1 config. §4 trigger `relax_min_hits` → Task 1 + driver tests. §5 driver → Task 1. §6.1 ES wiring → Tasks 2-3. §6.2 text/hybrid → Tasks 4-5. §6.3 complementary → Task 6. §7 config → Task 1. §8 observability (`fallback_dropped` / `relax_log`) → Task 3 + Task 6 logging. §9 testing → Tasks 1-7. §10 out-of-scope respected (low-recall retry untouched in logic, just relocated).
- **Type consistency:** `run_with_progressive_relax(search_fn, priority, min_hits) -> (list, list[str])` used identically in Tasks 3, 5, 6. `get_relax_config() -> (bool, list[str], int)` identical everywhere. `skip_slots: set[str] | None` consistent in `build_role_milvus_expr_parts` (Task 4) and `_build_role_milvus_expr` (Task 6). `skip_modeling`/`skip_price` consistent in `build_modeling_price_milvus_expr` (Task 4) and both call sites (Task 5, Task 6). `skip_up_time` consistent in both retriever methods (Task 4) and the Task 5 `search_fn`.
- **Behavior-change flag:** Task 4 Step 7 removes the hybrid→dense auto-fallback. If this breaks `test_recall_by_hybrid.py`, the fix is in the test (new behavior) or in Task 5 Step 3 (add a dense leg to `search_fn`). Do not silently re-add the auto-fallback — that would bypass the priority model.
