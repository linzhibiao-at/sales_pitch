"""同 role 多 SKU 搭配按笛卡尔积拆分回归测试。

锁定 dphs / outfits_unique 两个 ETL 在写 ES 前从源头消除"一套搭配里同 role
多 sku"：源数据里同一 role 出现多件（同款不同色等）时，按 role 分组做笛卡尔积
拆成多套，每 role 取 1 件。镜像 build_fila_guide_outfits_fast 的拆分口径。
"""

from __future__ import annotations

import unittest

from scripts.build_dphs_outfits_es import build_outfit_docs as dphs_build_docs
from scripts.build_outfits_unique_es import build_outfit_docs as unique_build_docs
from scripts.outfit_item_builder import (
    build_outfit_item_from_sku,
    cartesian_split_items_by_role,
)


def _item(sid: str, role: str) -> dict:
    """最小 SKU 记录 → outfit item（role 来自 skus.jsonl 归一化值）。"""
    return build_outfit_item_from_sku(
        sid,
        {"sku_id": sid, "role": role, "price": 100.0, "gender": ["女"], "season": ["夏"]},
        False,
    )


def _roles_in(combo: list[dict]) -> list[str]:
    return [it["role"] for it in combo]


class CartesianSplitTest(unittest.TestCase):
    def test_multi_role_multi_variant_splits_into_cartesian(self):
        # 2 裤 + 2 鞋 + 1 上衣 → 2*2*1 = 4 套，每 role 各 1 件
        items = [
            _item("P1", "bottoms"), _item("P2", "bottoms"),
            _item("S1", "shoes"), _item("S2", "shoes"),
            _item("T1", "top"),
        ]
        combos = cartesian_split_items_by_role(items)
        self.assertEqual(len(combos), 4)
        for combo in combos:
            roles = _roles_in(combo)
            self.assertEqual(len(roles), len(set(roles)))  # 每 role 至多 1 件
            self.assertEqual(sorted(roles), ["bottoms", "shoes", "top"])

    def test_single_variant_per_role_no_split(self):
        items = [_item("T1", "top"), _item("P1", "bottoms"), _item("S1", "shoes")]
        combos = cartesian_split_items_by_role(items)
        self.assertEqual(len(combos), 1)
        self.assertEqual(sorted(_roles_in(combos[0])), ["bottoms", "shoes", "top"])

    def test_single_role_dropped(self):
        # 全部同 role（3 裤）无法成多 role 搭配 → []
        items = [_item("P1", "bottoms"), _item("P2", "bottoms"), _item("P3", "bottoms")]
        self.assertEqual(cartesian_split_items_by_role(items), [])

    def test_role_of_fallback(self):
        # role 缺失时用 role_of 回退分组（outfits_unique 鞋类 role 常为空）
        items = [
            {"sku_id": "A", "role": ""}, {"sku_id": "B", "role": ""},  # 同回退 role
            {"sku_id": "C", "role": "shoes"},
        ]
        parsed = {"A": "top", "B": "top", "C": "shoes"}
        combos = cartesian_split_items_by_role(
            items, role_of=lambda it: it.get("role") or parsed.get(it.get("sku_id"), "")
        )
        # top[A,B] × shoes[C] = 2 套
        self.assertEqual(len(combos), 2)

    def test_combos_are_independent_copies(self):
        items = [_item("P1", "bottoms"), _item("P2", "bottoms"), _item("S1", "shoes")]
        combos = cartesian_split_items_by_role(items)
        # S1 同时出现在两套里；改一套的 S1 不应影响另一套（浅拷贝隔离）
        s1_in_combo0 = next(it for it in combos[0] if it["sku_id"] == "S1")
        s1_in_combo1 = next(it for it in combos[1] if it["sku_id"] == "S1")
        self.assertFalse(s1_in_combo0 is s1_in_combo1)
        s1_in_combo0["isMaster"] = True
        self.assertFalse(s1_in_combo1.get("isMaster"))

    def test_max_combos_cap(self):
        # 2 role × 各 20 件 = 400 > 200 → per-group cap 截断
        items = [_item(f"P{i}", "bottoms") for i in range(20)] + [
            _item(f"S{i}", "shoes") for i in range(20)
        ]
        combos = cartesian_split_items_by_role(items, max_combos=200)
        self.assertLessEqual(len(combos), 400)
        # 截断后每组合仍每 role 1 件、≥2 件
        for combo in combos:
            roles = _roles_in(combo)
            self.assertEqual(len(roles), len(set(roles)))


class UniqueBuildDocsTest(unittest.TestCase):
    """复现 outfits_unique.txt 第 3124 行：2 外套 + 2 裤 + 2 鞋 → 8 套。"""

    def _row(self) -> dict:
        return {
            "outfit_id": "outfits_unique_BASE",
            "sku_ids": [
                "F11W628505FDB", "F11W628501FWT",  # 外套 ×2
                "F11W628610FDB", "F11W628605FWT",  # 裤 ×2
                "F12W621119FSN", "F12W621119FPC",  # 鞋 ×2
            ],
            "roles": ["外套", "外套", "裤子", "裤子", "鞋", "鞋"],
            "tags": [],
            "reason": "",
            "raw_line": "外套F11W628505FDB-外套F11W628501FWT-裤子F11W628610FDB-裤子F11W628605FWT-鞋F12W621119FSN-鞋F12W621119FPC",
        }

    def _sku_details(self) -> dict:
        roles = ["top", "top", "bottoms", "bottoms", "shoes", "shoes"]
        out = {}
        for sid, role in zip(self._row()["sku_ids"], roles):
            out[sid] = {
                "sku_id": sid, "role": role, "price": 100.0,
                "gender": ["女"], "season": ["夏"], "spu_id": sid[: -3],
            }
        return out

    def test_splits_into_eight_role_pure_outfits(self):
        docs = unique_build_docs(self._row(), self._sku_details())
        self.assertEqual(len(docs), 8)
        seen_ids = set()
        for doc in docs:
            roles = [it["role"] for it in doc["items"]]
            self.assertEqual(len(roles), len(set(roles)))  # 无同 role 多 sku
            self.assertEqual(sorted(roles), ["bottoms", "shoes", "top"])
            self.assertEqual(len(doc["sku_ids"]), 3)
            self.assertEqual(doc["master_sku_id"], doc["items"][0]["sku_id"])
            self.assertTrue(doc["items"][0]["isMaster"])
            self.assertNotIn(doc["outfit_id"], seen_ids)  # 每套 id 唯一
            seen_ids.add(doc["outfit_id"])

    def test_single_variant_backward_compatible_id(self):
        row = {
            "outfit_id": "outfits_unique_BASE",
            "sku_ids": ["T1", "P1", "S1"],
            "roles": ["上衣", "裤子", "鞋"],
            "tags": [], "reason": "", "raw_line": "上衣T1-裤子P1-鞋S1",
        }
        details = {
            "T1": {"sku_id": "T1", "role": "top", "price": 1.0, "gender": ["女"], "season": ["夏"], "spu_id": "T"},
            "P1": {"sku_id": "P1", "role": "bottoms", "price": 1.0, "gender": ["女"], "season": ["夏"], "spu_id": "P"},
            "S1": {"sku_id": "S1", "role": "shoes", "price": 1.0, "gender": ["女"], "season": ["夏"], "spu_id": "S"},
        }
        docs = unique_build_docs(row, details)
        self.assertEqual(len(docs), 1)
        # 无同 role 重复时退化为单套，outfit_id 由 SKU 组合哈希生成（稳定）
        self.assertEqual(docs[0]["sku_ids"], ["T1", "P1", "S1"])


class DphsBuildDocsTest(unittest.TestCase):
    def test_multi_variant_splits_with_suffix_id(self):
        row = {
            "outfit_id": "DPHS_001",
            "sku_ids": ["P1", "P2", "S1"],
            "tags": ["通勤"], "reason": "test",
        }
        details = {
            "P1": {"sku_id": "P1", "role": "bottoms", "price": 1.0, "gender": ["女"], "season": ["夏"], "spu_id": "P"},
            "P2": {"sku_id": "P2", "role": "bottoms", "price": 1.0, "gender": ["女"], "season": ["夏"], "spu_id": "P"},
            "S1": {"sku_id": "S1", "role": "shoes", "price": 1.0, "gender": ["女"], "season": ["夏"], "spu_id": "S"},
        }
        docs = dphs_build_docs(row, details)
        self.assertEqual(len(docs), 2)  # 裤[P1,P2] × 鞋[S1]
        # idx 0 保持原 id（向后兼容），idx 1 带后缀
        self.assertEqual(docs[0]["outfit_id"], "DPHS_001")
        self.assertEqual(docs[1]["outfit_id"], "DPHS_001__c1")
        for doc in docs:
            roles = [it["role"] for it in doc["items"]]
            self.assertEqual(len(roles), len(set(roles)))
        # dphs 特有字段保留
        self.assertEqual(docs[0]["dphs_reason"], "test")
        self.assertEqual(docs[0]["occasion_tags"], ["通勤"])


if __name__ == "__main__":
    unittest.main()
