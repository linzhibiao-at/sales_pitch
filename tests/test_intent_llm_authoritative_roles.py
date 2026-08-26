"""意图识别角色解析：LLM 权威，正则不得 shadow。

回归用例：文本「搭配网球裤和白色网球鞋」+ SKU 锚点。LLM 正确返回
target_roles=['bottoms','shoes']，但旧 resolve_roles 正则在单字「配」处误切，
只抽出「鞋」并 shadow 掉 LLM 结果 → bottoms 丢失、裤子不召回。
"""

from __future__ import annotations

import unittest
from unittest import mock

from backend import llm_client
from backend.intent.intent_engine import extract_intent


def _sku_row(sku_id: str = "F11W619219FPK", **kw) -> dict:
    base = {
        "sku_id": sku_id,
        "gender": "女",
        "age": "",
        "season": ["春"],
        "role": "top",
        "category_l2": "长袖T恤",
        "length_class": "long",
        "coverage": "upper",
        "scene_domain": "daily",
        "series": "FILA ORIGINALE",
    }
    base.update(kw)
    return base


# 模拟 LLM 对「搭配网球裤和白色网球鞋」的权威返回
_LLM_RAW = {
    "query_type": "item_to_outfit",
    "anchor_role": "top",
    "target_roles": ["bottoms", "shoes"],
    "gender": "女",
    "season": ["春"],
    "category": ["长袖T恤"],
    "scene_domain": "daily",
    "series": "FILA ORIGINALE",
    "target_slots": {
        "bottoms": {"positive": {"category": ["梭织长裤", "针织长裤"], "scene_domain": "tennis"}, "negative": {}},
        "shoes": {"positive": {"color": ["白色"], "category": ["网球鞋"], "scene_domain": "tennis"}, "negative": {}},
    },
}


class LlmAuthoritativeRolesTest(unittest.TestCase):
    def test_llm_target_roles_not_shadowed_by_regex(self):
        with mock.patch.object(llm_client, "extract_intent_json", return_value=_LLM_RAW):
            res = extract_intent("搭配网球裤和白色网球鞋", sku_input_row=_sku_row())
        # LLM 给了 ['bottoms','shoes']，正则不该把它覆盖成 ['shoes']
        self.assertEqual(
            sorted(res.intent.target_roles), ["bottoms", "shoes"],
            f"target_roles 被 shadow：{res.intent.target_roles}",
        )

    def test_llm_target_slots_preserved(self):
        with mock.patch.object(llm_client, "extract_intent_json", return_value=_LLM_RAW):
            res = extract_intent("搭配网球裤和白色网球鞋", sku_input_row=_sku_row())
        ts = res.intent.target_slots or {}
        self.assertIn("bottoms", ts, "bottoms per-role 正向槽位丢失")
        self.assertEqual(ts["bottoms"]["positive"].get("scene_domain"), "tennis")


if __name__ == "__main__":
    unittest.main()
