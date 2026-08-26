# Progressive Slot-Filter Relaxation — Design

**Date:** 2026-07-22
**Status:** Draft, pending review
**Scope:** `fila_agent_html` retrieval layer

## 1. Problem

User-text-parsed slots plus default slots (gender, season, …) are organized into ES /
Milvus queries across three recall pathways. When a query returns **0 products** for a
role, there is no general mechanism to loosen filters progressively. What exists today
is partial and hardcoded:

- **ES path** drops `category_l2` → `color_series` → `modeling`
  (`outfit_recall.py:1193-1328`).
- **Milvus text/hybrid path** drops `category_l2` → `color_series`
  (`sku_retriever.py:303-339`, hybrid counterpart `:432-448`).
- **Complementary (image-vector) Milvus path** has **no** relaxation
  (`complementary_recall.py:191`).
- `gender`, `season`, `age`, `scene_domain`, `series`, `budget`, `up_time`, and
  per-role positive/negative slots are treated as hard — never dropped on 0 hits.
- There is no priority data structure; each drop is individually coded.

We want a single, priority-ordered, configurable relaxation: when a role yields 0 hits,
drop the soft slots **most-specific-first** until non-empty, but never touch the hard
identity/business slots.

## 2. Hard vs soft boundary

**Hard — never dropped, even at 0 results (the relaxation stops here):**

| Slot     | Why hard                                  |
|----------|-------------------------------------------|
| `gender` | user-self-reported identity              |
| `season` | user-self-reported season                |
| `age`    | user-self-reported age band              |

> **Decision (option 2):** `up_time` and `price` are **soft**, placed at the **tail** of
> `relax_priority` — relaxed only after every product-attribute soft slot has been
> sacrificed and the role is still empty. Rationale: prefer widening the recency floor /
> budget before returning 0 to the user. Order within the tail: `up_time` before `price`
> (loosen the time floor first, then budget). Identity slots (`gender`/`season`/`age`)
> stay hard — relaxing them risks returning cross-gender/cross-season results.

**Soft — relaxable, dropped in priority order:** `modeling`; per-role positive
`length_class`, `coverage`; anchor-isolation `series`, `scene_domain`; pairing
`color_series`, `category_l2`; anchor-attr `must_not` (`is_intimate`/layer); and the
tail business constraints `up_time`, `price`.

## 3. Priority list (drop first → last)

```
recommend.relax_priority:
  [modeling, length_class, coverage, series, scene_domain,
   color_series, category_l2, anchor_attr_must_not,
   up_time, price]
```

- `modeling` dropped first (most often the 0-hit culprit under intimate intent).
- `anchor_attr_must_not` dropped last among the product-attribute softs (structural
  guard, not user intent).
- `up_time` then `price` at the **tail** — widened only after all product-attribute
  softs are exhausted and the role is still empty.
- `category_l2`/`color_series` positions updated from the old ladder; the old hardcoded
  ladder is **replaced wholesale** by this loop, not appended to.
- Hard slots (`gender`/`season`/`age`) are **never members** of this list; the loop
  cannot reach them.
- The list is config — reorder, subset, or empty (== disable relaxation) without code
  change.

## 4. Trigger condition

Relax a slot when **that role's hit count < `recommend.relax_min_hits`** after the
current query. `relax_min_hits` defaults to `1` (i.e. trigger on 0 hits), matching
today's ES `not picked` and Milvus `not rows` semantics.

The existing **low-recall retry** (`recall_text_vector_skus` `outfit_recall.py:929-954`:
when `< 3` hits, lower `min_similarity` by 0.15 and re-search, keeping all filters) stays
**separate and untouched**. It handles "few but non-zero"; progressive relaxation handles
"zero". They compose: relaxation drops filters at 0; low-recall lowers threshold at <3.

## 5. Driver (shared, per pathway)

One small, pathway-agnostic driver runs the loop. Each pathway supplies its own
`search_fn(role, dropped_set) -> hits` that knows how to clear a given slot.

```
def run_with_progressive_relax(search_fn, priority, min_hits):
    dropped = []
    hits = search_fn(set())                      # full query first
    for slot in priority:
        if len(hits) >= min_hits:
            return hits, dropped
        dropped.append(slot)
        hits = search_fn(set(dropped))            # re-run with these slots cleared
    return hits, dropped                         # exhausted softs → stop at hard wall
```

Properties:
- Always runs the full query once first (no regression when non-empty).
- `dropped` is the ordered list of slots actually sacrificed (observability).
- Hard wall is implicit: hard slots (`gender`/`season`/`age`) aren't in `priority`, so
  the loop ends after the last soft slot (`price`) and returns whatever the
  identity-only query yields (possibly still empty).
- Re-runs stay within-pathway; **no cross-pathway coordination** (pathways serve
  different roles and `multi_path_recall` merges outputs).

## 6. Per-pathway wiring

Each pathway's query builder already takes skip-knobs. The driver flips them per
`relax_priority`. New skip-knobs are added where missing.

### 6.1 ES (query2es)
- Replace the three sequential `if not picked` blocks
  (`outfit_recall.py:1193-1328`) with `run_with_progressive_relax`.
- `resolve_es_query_for_role` (`es_intent.py:450`) gains skip-flags for the newly
  relaxable slots, mirroring existing `skip_modeling` / `skip_color_series` /
  `allowed_companion_cat2=None`:
  - `skip_length_class`, `skip_coverage` (per-role positive — clear in
    `build_role_es_positive_filters` `role_slots.py:403`)
  - `skip_series` (clear `build_series_es_filter` clause, `outfit_conflict.py:636`)
  - `skip_scene_domain` (clear `build_scene_domain_es_filter`, `outfit_conflict.py:555`)
  - `skip_anchor_attr_must_not` (clear `build_attr_es_filter`, `outfit_conflict.py:494`)
  - `skip_up_time` (clear `build_up_time_es_filter`, `up_time_filter.py:54`)
  - `skip_price` (clear the budget `range price gte/lte` clause in
    `_build_intent_filters` `es_intent.py:332` and per-role `:636-643`)
- `meta["fallback_no_*"]` flags → single `meta["fallback_dropped"] = dropped`.

### 6.2 Milvus text/hybrid
- Replace the two recursive `fallback_on_empty` drops
  (`sku_retriever.py:303-339`) and the hybrid 0-hit fallback (`:432-448`) with the
  driver. The hybrid→dense-only fallback is retained as the *initial* `search_fn` leg;
  the driver then drops soft slots on top.
- `_build_expr` (`sku_retriever.py:200,386`) honors the same new skip-flags, clearing
  the corresponding `merge_milvus_expr` terms (incl. `build_up_time_milvus_expr`
  `up_time_filter.py:62` and the price terms in `build_modeling_price_milvus_expr`
  `role_slots.py:263`).

### 6.3 Complementary Milvus (image-vector)
- Wire the driver into `_search_one_role` (`complementary_recall.py:191`).
- `_build_role_milvus_expr` (`:38`) gains the same skip-flags, clearing:
  - per-role parts (`build_role_milvus_expr_parts`, `role_slots.py:209`)
  - series (`build_series_milvus_expr`, `outfit_conflict.py:590`)
  - scene_domain (`build_scene_domain_milvus_expr`, `outfit_conflict.py:460`)
  - anchor attr (`build_attr_milvus_expr`, `outfit_conflict.py:401`)
  - up_time (`build_up_time_milvus_expr`, `up_time_filter.py:62`)
  - price (`build_modeling_price_milvus_expr`, `role_slots.py:263`)
- This path currently has zero relaxation; the driver is its first.

## 7. Config additions (`config.yaml`, under `recommend:`)

```yaml
recommend:
  enable_progressive_relax: true   # master switch; false == today's exact behavior
  relax_priority: [modeling, length_class, coverage, series, scene_domain,
                   color_series, category_l2, anchor_attr_must_not,
                   up_time, price]
  relax_min_hits: 1                # per-role trigger threshold
```

- `enable_progressive_relax: false` restores the pre-change hardcoded ladders / no-op
  complementary path — safe rollback.
- Empty `relax_priority` == no relaxation (query runs once, identity-only on 0).

## 8. Observability & safety

- Per-role `meta["fallback_dropped"]` plus a top-level `relax_log` summarizing which
  slots were sacrificed per request, per pathway.
- Master switch defaults on but disables to today's behavior when off.
- **Hard-wall guarantee:** the loop only drops slots named in `relax_priority`;
  `gender`/`season`/`age` are never members, so they survive even when the chain
  exhausts (`up_time` and `price` are dropped last) and the role stays empty.
- `up_time`/`price` relaxation is **last-resort only** — they fire only when every
  product-attribute soft has already been dropped and the role is still empty, so the
  common path never widens recency floor or budget.
- No new external calls; re-runs reuse the same ES/Milvus clients and indexes.

## 9. Testing

- **Driver unit:** given a `search_fn` that returns 0 until slot k is dropped, assert
  `dropped == priority[:k]` and the loop stops at `min_hits`; assert it exhausts the
  list and stops at the hard wall without dropping anything outside `priority`; assert
  reordering `relax_priority` changes the drop order; assert empty `priority` runs once.
- **Per-pathway integration:** synthetic intent yielding 0 hits with all softs → verify
  each soft slot is cleared in `relax_priority` order until non-empty, and that
  `gender`/`season`/`age` survive even when the chain exhausts (incl. after `up_time`
  and `price` are dropped).
- **Tail-only guard:** assert `up_time` and `price` are **not** dropped when an earlier
  soft slot already yields ≥ `min_hits` (no eager recency/budget widening).
- **Regression:** with `enable_progressive_relax: false`, existing suite unchanged.

## 10. Out of scope

- Cross-pathway relaxation coordination (pathways relax independently).
- Relaxing `gender`/`season`/`age` (identity) — held back by design; revisit only if
  recall targets demand it.
- The low-recall `<3` similarity retry — left as-is.
- Changes to slot *parsing* or *extraction* — this design only loosens filters at recall
  time.

## 11. Key file reference

- `backend/services/outfit_recall.py` — `recall_query2es_skus._process_one_role` :1121
  (ES ladder to replace), `recall_text_vector_skus` :797, `multi_path_recall` :1487.
- `backend/retrieval/es_intent.py` — `resolve_es_query_for_role` :450.
- `backend/retrieval/sku_retriever.py` — `recall_by_text_vector_keywords` :120,
  `recall_by_hybrid` :343, `_build_expr` :200/:386.
- `backend/services/complementary_recall.py` — `_build_role_milvus_expr` :38,
  `_search_one_role` :191.
- `backend/intent/role_slots.py` — slot model + per-role builders.
- `backend/ranking/outfit_conflict.py` — anchor-isolation / attr filter builders.
- `config.yaml` — `recommend.*` block.
