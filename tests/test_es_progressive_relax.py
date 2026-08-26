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


def _filter_fields(es_query: dict) -> set[str]:
    """收集 bool.filter / must_not 里出现的字段名，用于断言某字段是否被下推。

    返回的 es_query 顶层结构为 ``{"bool": {"must":[...], "filter":[...],
    "should":[...], "must_not":[...]}}``。
    """
    fields: set[str] = set()
    b = es_query.get("bool", {})
    for clause in (b.get("filter") or []):
        for k in (clause or {}):
            inner = clause[k]
            if isinstance(inner, dict):
                fields.update(inner.keys())
    for clause in (b.get("must_not") or []):
        for k in (clause or {}):
            inner = clause[k]
            if isinstance(inner, dict):
                fields.update(inner.keys())
    return fields


class EsSkipFlagTest(unittest.TestCase):
    def _q(self, **skip) -> dict:
        q, _ = resolve_es_query_for_role(
            _intent(), "bottoms",
            anchor_row={"role": "top"}, llm_enabled=False, **skip,
        )
        return q

    def test_skip_up_time_removes_up_time_range(self):
        base = _filter_fields(self._q())
        self.assertIn("up_time", base)
        relaxed = _filter_fields(self._q(skip_up_time=True))
        self.assertNotIn("up_time", relaxed)

    def test_skip_price_removes_price_range(self):
        base = _filter_fields(self._q())
        self.assertIn("price", base)
        relaxed = _filter_fields(self._q(skip_price=True))
        self.assertNotIn("price", relaxed)

    def test_skip_modeling_removes_modeling_terms(self):
        i = UserIntent(
            anchor_role="top", target_roles=["bottoms"],
            target_slots={"bottoms": {"positive": {"modeling": "宽松"}}},
        )
        q, _ = resolve_es_query_for_role(
            i, "bottoms", anchor_row={"role": "top"}, llm_enabled=False,
        )
        self.assertIn("modeling", _filter_fields(q))
        q2, _ = resolve_es_query_for_role(
            i, "bottoms", anchor_row={"role": "top"}, llm_enabled=False, skip_modeling=True,
        )
        self.assertNotIn("modeling", _filter_fields(q2))

    def test_skip_anchor_attr_must_not_removes_is_intimate(self):
        base = _filter_fields(self._q())
        self.assertIn("is_intimate", base)  # build_attr_es_filter default
        relaxed = _filter_fields(self._q(skip_anchor_attr_must_not=True))
        self.assertNotIn("is_intimate", relaxed)

    def test_skip_scene_domain_removes_scene_domain(self):
        base = _filter_fields(self._q())
        self.assertIn("scene_domain", base)
        relaxed = _filter_fields(self._q(skip_scene_domain=True))
        self.assertNotIn("scene_domain", relaxed)


if __name__ == "__main__":
    unittest.main()


# ── _es_relax_kwargs 单元测试 ──────────────────────────────────────────────
from backend.services.outfit_recall import _es_relax_kwargs


class EsRelaxKwargsTest(unittest.TestCase):
    def test_empty_dropped_returns_base_kwargs(self):
        kw = _es_relax_kwargs(
            set(), allowed_cat2=["裤"], allowed_cs_role=["米色"], enable_cs=True,
        )
        self.assertEqual(kw["allowed_companion_cat2"], ["裤"])
        self.assertEqual(kw["allowed_companion_color_series"], ["米色"])
        self.assertFalse(kw["skip_color_series"])
        # no skip flags set
        for flag in ("skip_modeling", "skip_up_time", "skip_price", "skip_series"):
            self.assertNotIn(flag, kw)

    def test_color_series_drop_clears_both_companion_and_flag(self):
        kw = _es_relax_kwargs(
            {"color_series"}, allowed_cat2=["裤"], allowed_cs_role=["米色"], enable_cs=True,
        )
        self.assertIsNone(kw["allowed_companion_color_series"])
        self.assertTrue(kw["skip_color_series"])

    def test_category_l2_drop_clears_companion_cat2(self):
        kw = _es_relax_kwargs(
            {"category_l2"}, allowed_cat2=["裤"], allowed_cs_role=None, enable_cs=True,
        )
        self.assertIsNone(kw["allowed_companion_cat2"])

    def test_each_soft_slot_sets_its_flag(self):
        kw = _es_relax_kwargs(
            {"modeling", "length_class", "coverage", "series", "scene_domain",
             "anchor_attr_must_not", "up_time", "price"},
            allowed_cat2=None, allowed_cs_role=None, enable_cs=False,
        )
        self.assertTrue(kw["skip_modeling"])
        self.assertTrue(kw["skip_length_class"])
        self.assertTrue(kw["skip_coverage"])
        self.assertTrue(kw["skip_series"])
        self.assertTrue(kw["skip_scene_domain"])
        self.assertTrue(kw["skip_anchor_attr_must_not"])
        self.assertTrue(kw["skip_up_time"])
        self.assertTrue(kw["skip_price"])

    def test_hard_slots_not_mapped(self):
        # gender/season/age must never produce a skip flag
        kw = _es_relax_kwargs(
            {"gender", "season", "age"},
            allowed_cat2=None, allowed_cs_role=None, enable_cs=True,
        )
        # only base kwargs present, no skip_* flags
        self.assertEqual(
            set(kw.keys()),
            {"allowed_companion_cat2", "allowed_companion_color_series", "skip_color_series"},
        )

    def test_enable_cs_false_always_skips_color_series(self):
        kw = _es_relax_kwargs(set(), allowed_cat2=None, allowed_cs_role=None, enable_cs=False)
        self.assertTrue(kw["skip_color_series"])


# ── recall_query2es_skus 端到端 progressive relax 集成测试 ───────────────────
from unittest import mock


class _FakeEs:
    """available=True；search 按当前 query 是否含 modeling 决定返回空或 1 命中。"""
    def __init__(self):
        self.calls: list[dict] = []

    @property
    def available(self):
        return True

    def search_skus_with_query(self, q, n):
        self.calls.append(q)
        b = q.get("bool", {})
        fields = set()
        for c in (b.get("filter") or []):
            for k in (c or {}):
                inner = c[k]
                if isinstance(inner, dict):
                    fields.update(inner.keys())
        # 含 modeling 过滤 → 0 命中；放宽后 → 1 命中
        if "modeling" in fields:
            return []
        return [("S1", 1.0)]


class EsRelaxIntegrationTest(unittest.TestCase):
    def _make_sku_r(self, fake_es):
        sku_r = mock.MagicMock()
        sku_r._es = fake_es
        sku_r.get_sku.return_value = {"sku_id": "S1", "category_l2": "裤", "title": "x", "role": "bottoms"}
        return sku_r

    def test_drops_modeling_on_zero_hits(self):
        from backend.services import outfit_recall as orc
        fake_es = _FakeEs()
        sku_r = self._make_sku_r(fake_es)
        intent = UserIntent(
            anchor_role="top", target_roles=["bottoms"], gender="男",
            target_slots={"bottoms": {"positive": {"modeling": "宽松"}}},
        )
        cfg = {
            "recommend": {
                "query2es_llm_enabled": False,
                "enable_category_l2_pairing": False,
                "enable_color_series_pairing": False,
                "es_top_n_per_role": 10,
            }
        }
        with mock.patch("backend.services.outfit_recall.load_config", return_value=cfg), \
             mock.patch("backend.services.outfit_recall.get_elasticsearch_indices",
                        return_value={"skus": "fila_skus"}), \
             mock.patch("backend.services.outfit_recall.es_top_n_per_role", return_value=10), \
             mock.patch("backend.services.outfit_recall.resolve_pairing_allowed_companions",
                        return_value=None), \
             mock.patch("backend.services.outfit_recall.get_relax_config",
                        return_value=(True, ["modeling"], 1)), \
             mock.patch("backend.services.outfit_recall._debug_recall_io_enabled",
                        return_value=False):
            by_role, meta = orc.recall_query2es_skus(sku_r, intent, {"role": "top", "sku_id": "A1"})
        self.assertIn("bottoms", by_role)
        self.assertEqual(by_role["bottoms"][0]["sku_id"], "S1")
        self.assertEqual(meta["bottoms"].get("fallback_dropped"), ["modeling"])
        # 第一轮含 modeling（0 命中），第二轮不含（命中）
        self.assertGreaterEqual(len(fake_es.calls), 2)

    def test_master_switch_off_no_fallback_dropped(self):
        from backend.services import outfit_recall as orc
        fake_es = _FakeEs()  # always 0 on modeling, but switch off → single call, no drop
        sku_r = self._make_sku_r(fake_es)
        intent = UserIntent(
            anchor_role="top", target_roles=["bottoms"], gender="男",
            target_slots={"bottoms": {"positive": {"modeling": "宽松"}}},
        )
        cfg = {"recommend": {"query2es_llm_enabled": False,
                             "enable_category_l2_pairing": False,
                             "enable_color_series_pairing": False,
                             "es_top_n_per_role": 10}}
        with mock.patch("backend.services.outfit_recall.load_config", return_value=cfg), \
             mock.patch("backend.services.outfit_recall.get_elasticsearch_indices",
                        return_value={"skus": "fila_skus"}), \
             mock.patch("backend.services.outfit_recall.es_top_n_per_role", return_value=10), \
             mock.patch("backend.services.outfit_recall.resolve_pairing_allowed_companions",
                        return_value=None), \
             mock.patch("backend.services.outfit_recall.get_relax_config",
                        return_value=(False, ["modeling"], 1)), \
             mock.patch("backend.services.outfit_recall._debug_recall_io_enabled",
                        return_value=False):
            by_role, meta = orc.recall_query2es_skus(sku_r, intent, {"role": "top", "sku_id": "A1"})
        self.assertNotIn("bottoms", by_role)  # 0 hits, no relax → empty
        self.assertNotIn("fallback_dropped", meta.get("bottoms", {}))
        self.assertEqual(len(fake_es.calls), 1)  # single call, no loop
