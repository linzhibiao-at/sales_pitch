"""text_vector 通路低召回降阈值二次召回（B）测试。

验证：某 role 首次召回 <3 条时，用更低 min_similarity 二次召回，合并结果。
"""

from __future__ import annotations

import unittest
from unittest import mock

from backend.models import UserIntent
from backend.services.outfit_recall import recall_text_vector_composed_outfits


class _FakeSkuRetriever:
    """记录 recall 调用，按 min_similarity_override 返回不同结果。"""

    def __init__(self, rows_by_sid: dict):
        self._rows = rows_by_sid
        self.calls: list[dict] = []

    def recall_by_text_vector_keywords(self, keywords, top_k_per_keyword=None, **kw):
        override = kw.get("min_similarity_override")
        self.calls.append({"keywords": keywords, "role": kw.get("role_filter"), "override": override})
        if override is None:
            # 首次：只 1 条（触发降阈值）
            return [("SKU_LOW_1", 0.57, 0.57)]
        # 二次（降阈值）：多几条
        return [
            ("SKU_LOW_1", 0.57, 0.57),
            ("SKU_RETRY_2", 0.45, 0.45),
            ("SKU_RETRY_3", 0.42, 0.42),
        ]

    def get_sku(self, sid):
        return self._rows.get(sid)


class TextVectorLowRecallRetryTest(unittest.TestCase):
    def test_low_recall_triggers_lower_threshold_retry(self):
        intent = UserIntent(
            anchor_role="top", target_roles=["bottoms", "shoes"],
            gender="女", season=["秋"],
            target_slots={
                "bottoms": {"positive": {"color": ["粉色"], "color_series": ["粉色系"]}, "negative": {}},
                "shoes": {"positive": {"color": ["白色"], "color_series": ["白色系"]}, "negative": {}},
            },
        )
        anchor_row = {"sku_id": "ANCHOR_TOP", "role": "top", "category_l2": "短袖编织衫",
                      "scene_domain": "daily", "color_series": ["白色系"]}
        rows = {
            # 首次召回的 LOW_1 是夏季 → 被 season 过滤剔除（模拟唯一粉色裤子是夏款）
            "SKU_LOW_1": {"sku_id": "SKU_LOW_1", "role": "bottoms", "category_l2": "梭织长裤",
                          "color_series": ["粉色系"], "season": ["夏"], "gender": ["女"], "length_class": "long",
                          "scene_domain": "daily", "is_intimate": False},
            # 二次降阈值召回捞回的秋款粉色长裤（不同品类，过 dedupe）
            "SKU_RETRY_2": {"sku_id": "SKU_RETRY_2", "role": "bottoms", "category_l2": "针织长裤",
                            "color_series": ["粉色系"], "season": ["秋"], "gender": ["女"], "length_class": "long",
                            "scene_domain": "daily", "is_intimate": False},
            "SKU_RETRY_3": {"sku_id": "SKU_RETRY_3", "role": "bottoms", "category_l2": "梭织七分裤",
                            "color_series": ["粉色系"], "season": ["秋"], "gender": ["女"], "length_class": "long",
                            "scene_domain": "daily", "is_intimate": False},
        }
        # shoes 给足量，不触发 retry
        for sid in ("SHOE1", "SHOE2", "SHOE3", "SHOE4"):
            rows[sid] = {"sku_id": sid, "role": "shoes", "category_l2": "运动鞋",
                         "color_series": ["白色系"], "season": ["秋"], "gender": ["女"],
                         "length_class": "n/a", "scene_domain": "daily", "is_intimate": False}

        sku_r = _FakeSkuRetriever(rows)
        # shoes 首次需返回 >=3 不触发 retry；bottoms 返回 1 触发 retry
        orig_recall = sku_r.recall_by_text_vector_keywords
        call_log: list[dict] = []

        def _recall(keywords, top_k_per_keyword=None, **kw):
            override = kw.get("min_similarity_override")
            role = kw.get("role_filter")
            call_log.append({"role": role, "override": override})
            if role == "bottoms" and override is None:
                return [("SKU_LOW_1", 0.57, 0.57)]
            if role == "bottoms" and override is not None:
                return [("SKU_LOW_1", 0.57, 0.57),
                        ("SKU_RETRY_2", 0.45, 0.45),
                        ("SKU_RETRY_3", 0.42, 0.42)]
            # shoes：返回 4 条不触发 retry
            return [(sid, 0.6, 0.6) for sid in ("SHOE1", "SHOE2", "SHOE3", "SHOE4")]

        sku_r.recall_by_text_vector_keywords = _recall  # type: ignore

        # 强制 dense 通路（本测试专测 dense 低召回降阈值二次召回；线上 config 可能是 hybrid）
        from backend.config import load_config as _real_load_config
        _cfg = _real_load_config()
        _cfg.setdefault("recommend", {})["text_recall_mode"] = "dense"
        with mock.patch(
            "backend.services.outfit_recall.load_config", return_value=_cfg
        ):
            outfits = recall_text_vector_composed_outfits(sku_r, intent, anchor_row, trace_id="t")

        # bottoms 应有 2 次调用（首次 + 降阈值二次）
        bottoms_calls = [c for c in call_log if c["role"] == "bottoms"]
        self.assertEqual(len(bottoms_calls), 2)
        self.assertIsNone(bottoms_calls[0]["override"])
        self.assertIsNotNone(bottoms_calls[1]["override"])
        # shoes 只 1 次（未触发 retry）
        shoes_calls = [c for c in call_log if c["role"] == "shoes"]
        self.assertEqual(len(shoes_calls), 1)
        # 结果应含 retry 捞回的秋款粉色裤子（LOW_1 夏季被 season 剔除，retry 捞回 RETRY_2/3）
        all_skus = set()
        for o in outfits:
            for it in o.get("items", []):
                all_skus.add(it.get("sku_id"))
        self.assertIn("SKU_RETRY_2", all_skus)
        # bottoms 角色出现在搭配中（修复前会被 season 全剔导致 2 件套）
        bottoms_in_outfits = [it.get("sku_id") for o in outfits for it in o.get("items", []) if it.get("role") == "bottoms"]
        self.assertTrue(bottoms_in_outfits, "应有 bottoms 出现")


if __name__ == "__main__":
    unittest.main()
