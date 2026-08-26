"""age（童装年龄段）字段接入检索链路的单元测试。

覆盖：
- normalize_age：合法值直通、别名归一、噪声/空 → None。
- age_conflict：指定段时空值/异段冲突、通码命中、未指定不冲突。
- _append_age_filter：ES terms 子句（含通码展开）/ term 子句 / 空跳过。
- backfill_intent_from_sku：空 age 时从 SKU 回填。
- _build_intent_from_llm：LLM raw.age 经归一落到 UserIntent.age。
- Trie 提取：ages.yaml 命中小童/中大童/婴幼童/通码及别名。
- _slots_to_intent：slots['age'] → intent.age。
"""

from __future__ import annotations

import unittest

from backend.models import UserIntent, normalize_age
from backend.query_understanding import backfill_intent_from_sku
from backend.ranking.scoring import age_conflict, intent_attr_match
from backend.retrieval.es_intent import _append_age_filter


class NormalizeAgeTest(unittest.TestCase):
    def test_canonical_passthrough(self):
        for v in ("小童", "中大童", "婴幼童", "通码"):
            self.assertEqual(normalize_age(v), v)

    def test_alias(self):
        self.assertEqual(normalize_age("大童"), "中大童")
        self.assertEqual(normalize_age("中童"), "中大童")
        self.assertEqual(normalize_age("婴儿"), "婴幼童")
        self.assertEqual(normalize_age("幼童"), "婴幼童")
        self.assertEqual(normalize_age("婴幼"), "婴幼童")

    def test_noise_and_empty(self):
        self.assertIsNone(normalize_age(None))
        self.assertIsNone(normalize_age(""))
        self.assertIsNone(normalize_age("  "))
        # 源端噪声（如误填的年龄数字）
        self.assertIsNone(normalize_age("33"))
        # 带空白 strip 后命中
        self.assertEqual(normalize_age("小童 "), "小童")


class AgeConflictTest(unittest.TestCase):
    def test_no_want_no_conflict(self):
        self.assertFalse(age_conflict("小童", None))
        self.assertFalse(age_conflict("小童", ""))

    def test_row_empty_is_conflict(self):
        # 查询指定童装段，行无 age（成人款/缺失）→ 冲突
        self.assertTrue(age_conflict("", "小童"))
        self.assertTrue(age_conflict(None, "中大童"))

    def test_same_segment_hit(self):
        self.assertFalse(age_conflict("小童", "小童"))
        self.assertFalse(age_conflict("中大童", "中大童"))

    def test_tongma_covers_all(self):
        # 通码 = 同款覆盖全段，任一查询段都应命中
        self.assertFalse(age_conflict("通码", "小童"))
        self.assertFalse(age_conflict("通码", "中大童"))
        self.assertFalse(age_conflict("通码", "婴幼童"))

    def test_different_segment_conflict(self):
        self.assertTrue(age_conflict("小童", "中大童"))
        self.assertTrue(age_conflict("中大童", "婴幼童"))

    def test_want_tongma(self):
        self.assertFalse(age_conflict("通码", "通码"))
        self.assertTrue(age_conflict("小童", "通码"))


class AppendAgeFilterTest(unittest.TestCase):
    def test_segment_expands_with_tongma(self):
        for seg in ("小童", "中大童", "婴幼童"):
            filters: list[dict] = []
            _append_age_filter(filters, seg)
            self.assertEqual(filters, [{"terms": {"age": [seg, "通码"]}}])

    def test_tongma_exact(self):
        filters: list[dict] = []
        _append_age_filter(filters, "通码")
        self.assertEqual(filters, [{"term": {"age": "通码"}}])

    def test_empty_skipped(self):
        filters: list[dict] = []
        _append_age_filter(filters, None)
        _append_age_filter(filters, "")
        _append_age_filter(filters, "   ")
        self.assertEqual(filters, [])


class IntentAttrMatchAgeTest(unittest.TestCase):
    def test_age_hit_adds_score(self):
        row = {"age": "小童"}
        self.assertAlmostEqual(
            intent_attr_match(row, None, [], [], age="小童"), 0.10
        )
        # 通码行命中任意查询段
        row2 = {"age": "通码"}
        self.assertAlmostEqual(
            intent_attr_match(row2, None, [], [], age="中大童"), 0.10
        )

    def test_age_miss_no_score(self):
        row = {"age": "中大童"}
        self.assertAlmostEqual(
            intent_attr_match(row, None, [], [], age="小童"), 0.0
        )
        # 行无 age 不加分
        self.assertAlmostEqual(
            intent_attr_match({}, None, [], [], age="小童"), 0.0
        )


class BackfillAgeFromSkuTest(unittest.TestCase):
    def test_backfill_when_empty(self):
        intent = UserIntent(text="板鞋")
        out = backfill_intent_from_sku(intent, {"age": "小童"})
        self.assertEqual(out.age, "小童")

    def test_alias_backfill(self):
        intent = UserIntent(text="鞋")
        out = backfill_intent_from_sku(intent, {"age": "大童"})
        self.assertEqual(out.age, "中大童")

    def test_no_override_when_present(self):
        intent = UserIntent(text="鞋", age="婴幼童")
        out = backfill_intent_from_sku(intent, {"age": "小童"})
        self.assertEqual(out.age, "婴幼童")

    def test_noise_not_backfilled(self):
        intent = UserIntent(text="鞋")
        out = backfill_intent_from_sku(intent, {"age": "33"})
        self.assertIsNone(out.age)


class BuildIntentFromLlmAgeTest(unittest.TestCase):
    def test_llm_age_normalized(self):
        from backend.intent.intent_engine import _build_intent_from_llm
        intent = _build_intent_from_llm("小童板鞋", {
            "query_type": "text_only",
            "text": "小童板鞋",
            "gender": "男童",
            "age": "大童",
            "season": ["夏"],
            "category": ["板鞋"],
        })
        self.assertEqual(intent.age, "中大童")

    def test_llm_age_null(self):
        from backend.intent.intent_engine import _build_intent_from_llm
        intent = _build_intent_from_llm("男款外套", {
            "query_type": "text_only",
            "text": "男款外套",
            "gender": "男",
            "season": ["冬"],
            "category": ["外套"],
        })
        self.assertIsNone(intent.age)


class TrieAgeExtractionTest(unittest.TestCase):
    def test_extract_age_tokens(self):
        from backend.intent.trie_extractor import get_multi_slot_extractor
        ext = get_multi_slot_extractor()
        slots = ext.extract_all("给我小童男童板鞋")
        self.assertEqual(slots.get("age"), ["小童"])
        slots = ext.extract_all("中大童的运动鞋")
        self.assertEqual(slots.get("age"), ["中大童"])
        slots = ext.extract_all("婴幼童连体衣")
        self.assertEqual(slots.get("age"), ["婴幼童"])

    def test_extract_age_alias(self):
        from backend.intent.trie_extractor import get_multi_slot_extractor
        ext = get_multi_slot_extractor()
        slots = ext.extract_all("大童T恤")
        self.assertEqual(slots.get("age"), ["中大童"])
        slots = ext.extract_all("婴儿套装")
        self.assertEqual(slots.get("age"), ["婴幼童"])

    def test_no_age_for_adult_query(self):
        from backend.intent.trie_extractor import get_multi_slot_extractor
        ext = get_multi_slot_extractor()
        slots = ext.extract_all("男款黑色外套")
        self.assertEqual(slots.get("age"), [])


class SlotsToIntentAgeTest(unittest.TestCase):
    def test_slots_age_to_intent(self):
        from backend.intent.intent_engine import _slots_to_intent
        intent = _slots_to_intent("小童板鞋", {"age": ["小童"]}, None, [])
        self.assertEqual(intent.age, "小童")

    def test_slots_age_alias_normalized(self):
        from backend.intent.intent_engine import _slots_to_intent
        intent = _slots_to_intent("大童鞋", {"age": ["大童"]}, None, [])
        self.assertEqual(intent.age, "中大童")


class InferAgeFromTitleTest(unittest.TestCase):
    """ETL 侧源 age 缺失时的 title 兜底推断（scripts.etl_common）。"""

    def test_canonical_in_title(self):
        from scripts.etl_common import infer_age_from_title
        self.assertEqual(infer_age_from_title("福袋001-男-小童"), "小童")
        self.assertEqual(infer_age_from_title("中大童运动鞋"), "中大童")
        self.assertEqual(infer_age_from_title("婴幼童连体衣"), "婴幼童")
        self.assertEqual(infer_age_from_title("通码T恤"), "通码")

    def test_alias_mapped(self):
        from scripts.etl_common import infer_age_from_title
        self.assertEqual(infer_age_from_title("福袋002-男-大童"), "中大童")
        self.assertEqual(infer_age_from_title("中童板鞋"), "中大童")
        self.assertEqual(infer_age_from_title("婴儿套装"), "婴幼童")
        self.assertEqual(infer_age_from_title("幼童卫衣"), "婴幼童")
        self.assertEqual(infer_age_from_title("婴幼款"), "婴幼童")

    def test_specificity_priority(self):
        # 「婴幼童」不应被「幼童」截短成同样结果（这里同桶，重点是不被「婴幼」误判前先命中长串）
        from scripts.etl_common import infer_age_from_title
        self.assertEqual(infer_age_from_title("中大童大童款"), "中大童")
        # 「中大童」优先于「大童」，结果同桶；「婴幼童」优先于「幼童」
        self.assertEqual(infer_age_from_title("婴幼童幼童"), "婴幼童")

    def test_adult_title_empty(self):
        from scripts.etl_common import infer_age_from_title
        # 成人款/无年龄关键词 → 不填（返回 ""），避免把成人款误判为童装
        self.assertEqual(infer_age_from_title("男款黑色外套"), "")
        self.assertEqual(infer_age_from_title("FILA儿童抱枕被"), "")  # 「儿童」非标准桶
        self.assertEqual(infer_age_from_title(""), "")


if __name__ == "__main__":
    unittest.main()
