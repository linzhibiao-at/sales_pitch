"""连衣裙 anchor × 固定搭配过滤回归测试。

覆盖两层防御：
1. ``outfit_conflict`` 的"连体/全身装 × 上下装冲突"规则对 ES 库中文角色
   token（上装/下装）也能生效（之前只认英文 top/bottoms，是死规则）。
2. ``recall_anchor_graph_outfits`` 用 ``intent.target_roles`` 过滤固定搭配：
   连衣裙 anchor 的 target_roles 为 [鞋, 配饰]，含上装/下装的固定搭配应被剔除。
"""

from __future__ import annotations

import unittest

from backend.intent.slot_defs import normalize_role
from backend.ranking.outfit_conflict import check_companion_conflict, check_outfit_conflict
from backend.services.outfit_recall import recall_anchor_graph_outfits


def _dress_anchor() -> dict:
    return {
        "sku_id": "D1",
        "spu_id": "SD1",
        "role": "连衣裙",
        "category_l2": "连衣裙",
        "title": "测试女子时尚休闲连衣裙",
        "gender": "女",
        "scene_domain": "daily",
    }


def _top_item() -> dict:
    return {
        "sku_id": "T1",
        "spu_id": "ST1",
        "role": "上装",
        "category_l2": "短袖T",
        "title": "女士短袖T恤",
        "gender": "女",
        "scene_domain": "daily",
    }


def _bottoms_item() -> dict:
    return {
        "sku_id": "P1",
        "spu_id": "SP1",
        "role": "下装",
        "category_l2": "梭织长裤",
        "title": "女士梭织长裤",
        "gender": "女",
        "scene_domain": "daily",
    }


def _shoes_item() -> dict:
    return {
        "sku_id": "SH1",
        "spu_id": "SSH1",
        "role": "鞋",
        "category_l2": "运动鞋",
        "title": "女士运动鞋",
        "gender": "女",
        "scene_domain": "daily",
    }


def _accessory_item() -> dict:
    return {
        "sku_id": "B1",
        "spu_id": "SB1",
        "role": "配饰",
        "category_l2": "挎包",
        "title": "女士挎包",
        "gender": "女",
        "scene_domain": "",
    }


class _FakeStore:
    """最小 store：提供 get_sku + outfits_by_skus_batch。"""

    def __init__(self, skus: dict[str, dict], by_sku: dict[str, list[dict]]):
        self._skus = skus
        self._by_sku = by_sku

    def get_sku(self, sku_id: str) -> dict | None:
        return self._skus.get(sku_id)

    def outfits_by_skus_batch(self, sku_ids, size: int = 200, sources=None):
        seen: set[str] = set()
        rows: list[dict] = []
        for sid in sku_ids:
            for o in self._by_sku.get(sid, []):
                oid = str(o.get("outfit_id") or o.get("idMatch") or "")
                if oid in seen:
                    continue
                seen.add(oid)
                rows.append(o)
        return rows


def _outfit(oid: str, items: list[dict]) -> dict:
    return {
        "outfit_id": oid,
        "items": items,
        "sku_ids": [it["sku_id"] for it in items],
        "gender": "女",
    }


class ConflictRoleNormalizationTest(unittest.TestCase):
    """fix 1：冲突规则对中文角色 token 生效。"""

    def test_dress_vs_chinese_top_role_conflicts(self):
        self.assertTrue(check_companion_conflict(_dress_anchor(), _top_item()))

    def test_dress_vs_chinese_bottoms_role_conflicts(self):
        self.assertTrue(check_companion_conflict(_dress_anchor(), _bottoms_item()))

    def test_dress_vs_shoes_no_conflict(self):
        self.assertFalse(check_companion_conflict(_dress_anchor(), _shoes_item()))

    def test_dress_vs_english_top_role_conflicts(self):
        """英文 token 仍能命中（回归保护）。"""
        top_en = dict(_top_item())
        top_en["role"] = "top"
        self.assertTrue(check_companion_conflict(_dress_anchor(), top_en))

    def test_check_outfit_conflict_rejects_outfit_with_top_and_bottoms(self):
        outfit_items = [_top_item(), _bottoms_item(), _shoes_item()]
        self.assertTrue(
            check_outfit_conflict(_dress_anchor(), outfit_items, anchor_id="D1")
        )

    def test_normalize_role_maps_zh_and_en(self):
        self.assertEqual(normalize_role("上装"), "top")
        self.assertEqual(normalize_role("下装"), "bottoms")
        self.assertEqual(normalize_role("top"), "top")
        self.assertEqual(normalize_role("鞋"), "shoes")
        self.assertEqual(normalize_role(""), "")


class AnchorGraphTargetRoleFilterTest(unittest.TestCase):
    """fix 2：anchor_graph 用 target_roles 剪枝非目标角色单品（不剔除整套）。"""

    def _store_with_neighbor_outfit(self, neighbor: dict, companions: list[dict]) -> _FakeStore:
        dress = _dress_anchor()
        outfit = _outfit(
            "O1",
            [neighbor, *companions],
        )
        return _FakeStore(
            skus={"D1": dress},
            by_sku={neighbor["sku_id"]: [outfit]},
        )

    def _non_anchor_roles(self, outfit: dict) -> set[str]:
        roles = set()
        for it in outfit.get("items", []):
            if it.get("is_anchor") or it.get("is_master"):
                continue
            r = normalize_role(it.get("role"))
            if r:
                roles.add(r)
        return roles

    def test_dress_outfit_pruned_but_missing_target_role_is_dropped(self):
        """连衣裙 anchor + target=[鞋,配饰]：移除上装/下装后仅剩配饰，缺鞋→丢弃整套。"""
        neighbor = dict(_bottoms_item())  # 近邻，会被替换为 anchor
        neighbor["sku_id"] = "N1"
        store = self._store_with_neighbor_outfit(neighbor, [_top_item(), _accessory_item()])
        outfits, _ = recall_anchor_graph_outfits(
            store,
            "D1",
            candidate_skus=["N1"],
            intent_target_roles=["鞋", "配饰"],
        )
        self.assertEqual(outfits, [], "剪枝后缺 target_role（鞋）应整套剔除")

    def test_dress_outfit_pruned_and_covers_all_target_roles_is_kept(self):
        """连衣裙 anchor + target=[鞋,配饰]：含上装/鞋/配饰，剪掉上装后仍覆盖鞋+配饰→保留。"""
        neighbor = dict(_bottoms_item())  # 近邻，会被替换为 anchor
        neighbor["sku_id"] = "N1"
        store = self._store_with_neighbor_outfit(
            neighbor, [_top_item(), _shoes_item(), _accessory_item()]
        )
        outfits, _ = recall_anchor_graph_outfits(
            store,
            "D1",
            candidate_skus=["N1"],
            intent_target_roles=["鞋", "配饰"],
        )
        self.assertEqual(len(outfits), 1, "覆盖所有 target_roles 应保留")
        kept = outfits[0]
        roles = self._non_anchor_roles(kept)
        self.assertNotIn("top", roles, "上装应被剪枝")
        self.assertIn("shoes", roles, "鞋应保留")
        self.assertIn("accessory", roles, "配饰应保留")

    def test_dress_outfit_with_only_shoes_accessory_is_kept(self):
        """连衣裙 anchor + target=[鞋,配饰]：非 anchor 项恰好为鞋/配饰→原样保留。"""
        # 近邻用上装（非 target），会被替换为连衣裙 anchor；鞋/配饰作为 companion 保留
        neighbor = dict(_top_item())
        neighbor["sku_id"] = "N1"
        store = self._store_with_neighbor_outfit(neighbor, [_shoes_item(), _accessory_item()])
        outfits, _ = recall_anchor_graph_outfits(
            store,
            "D1",
            candidate_skus=["N1"],
            intent_target_roles=["鞋", "配饰"],
        )
        self.assertEqual(len(outfits), 1)
        roles = self._non_anchor_roles(outfits[0])
        self.assertTrue(roles <= {"shoes", "accessory"})
        self.assertIn("shoes", roles)
        self.assertIn("accessory", roles)

    def test_dress_outfit_pruned_to_only_anchor_is_dropped(self):
        """剪枝后仅剩 anchor（不足 2 件）则放弃该搭配。"""
        neighbor = dict(_top_item())
        neighbor["sku_id"] = "N1"
        # 搭配 = [上装(近邻→替换为连衣裙), 下装]；target=[鞋,配饰] → 上下装均被剪枝
        store = self._store_with_neighbor_outfit(neighbor, [_bottoms_item()])
        outfits, _ = recall_anchor_graph_outfits(
            store,
            "D1",
            candidate_skus=["N1"],
            intent_target_roles=["鞋", "配饰"],
        )
        self.assertEqual(outfits, [], "剪枝后仅剩连衣裙应放弃")

    def test_no_target_roles_keeps_legacy_behavior(self):
        """未传 target_roles 时不剪枝（向后兼容）。"""
        neighbor = dict(_shoes_item())
        neighbor["sku_id"] = "N2"
        outfit = _outfit("O2", [neighbor, _accessory_item(), _top_item()])
        store = _FakeStore(
            skus={"D1": _dress_anchor()},
            by_sku={"N2": [outfit]},
        )
        outfits, _ = recall_anchor_graph_outfits(
            store,
            "D1",
            candidate_skus=["N2"],
            intent_target_roles=None,
        )
        # 未剪枝：上装仍在；但冲突规则(fix1) 会因连衣裙×上装剔除整套
        self.assertEqual(outfits, [], "未传 target_roles 时由冲突规则剔除 dress×上装")


class AnchorGraphRoleFallbackTest(unittest.TestCase):
    """fix 3：outfit item.role 缺失（ES 脏数据）时，用 SKU 行 role 兜底再判覆盖。

    复现线上 bug：micro_guide 搭配里鞋类单品 role=''（_build_item_dict 未持久化
    推断 role、ES 索引 _item_role 回退 upDown 对鞋为空），导致上装 anchor 的
    target_roles=[下装,鞋] 覆盖检查永远缺鞋，整套被丢、通路 0 召回。
    """

    def _store_with_empty_role_shoes(self, anchor: dict, companions: list[dict]) -> _FakeStore:
        outfit = _outfit("O_EMPTY_ROLE", [anchor, *companions])
        # SKU 行带正确 role（模拟 skus 索引）；outfit item 的 role 为空（脏数据）
        sku_rows = {anchor["sku_id"]: anchor}
        for c in companions:
            row = dict(c)
            row["role"] = c["sku_role"]  # SKU 行有正确 role
            sku_rows[c["sku_id"]] = row
        return _FakeStore(
            skus=sku_rows,
            by_sku={anchor["sku_id"]: [outfit]},
        )

    def test_top_anchor_outfit_with_empty_role_shoes_is_kept_via_sku_fallback(self):
        """上装 anchor + target=[下装,鞋]：outfit 里鞋 role='' 但 SKU 行 role=鞋→应保留。"""
        anchor = {
            "sku_id": "A1",
            "spu_id": "SA1",
            "role": "上装",
            "category_l2": "短袖T",
            "title": "女士短袖T恤",
            "gender": "女",
            "scene_domain": "daily",
        }
        bottoms = {
            "sku_id": "P1",
            "spu_id": "SP1",
            "sku_role": "下装",
            "role": "下装",
            "category_l2": "梭织短裤",
            "title": "女士梭织短裤",
            "gender": "女",
            "scene_domain": "daily",
        }
        shoes = {
            "sku_id": "SH1",
            "spu_id": "SSH1",
            "sku_role": "鞋",
            "role": "",  # 脏数据：outfit item role 为空
            "category_l2": "老爹鞋",
            "title": "女士老爹鞋",
            "gender": "女",
            "scene_domain": "daily",
        }
        store = self._store_with_empty_role_shoes(anchor, [bottoms, shoes])
        outfits, _ = recall_anchor_graph_outfits(
            store,
            "A1",
            candidate_skus=None,
            intent_target_roles=["下装", "鞋"],
        )
        self.assertEqual(len(outfits), 1, "鞋 role 缺失时应回退 SKU 行 role，保留整套")

    def test_top_anchor_outfit_dropped_when_no_sku_row_to_fallback(self):
        """无 SKU 行可回退时退回原行为（缺鞋→丢弃），不崩溃。"""
        anchor = {
            "sku_id": "A1",
            "spu_id": "SA1",
            "role": "上装",
            "category_l2": "短袖T",
            "title": "女士短袖T恤",
            "gender": "女",
            "scene_domain": "daily",
        }
        bottoms = {"sku_id": "P1", "spu_id": "SP1", "role": "下装",
                   "category_l2": "梭织短裤", "title": "女士短裤",
                   "gender": "女", "scene_domain": "daily"}
        shoes = {"sku_id": "SH1", "spu_id": "SSH1", "role": "",
                 "category_l2": "老爹鞋", "title": "女士老爹鞋",
                 "gender": "女", "scene_domain": "daily"}
        outfit = _outfit("O_NO_ROW", [anchor, bottoms, shoes])
        store = _FakeStore(skus={"A1": anchor}, by_sku={"A1": [outfit]})
        outfits, _ = recall_anchor_graph_outfits(
            store,
            "A1",
            candidate_skus=None,
            intent_target_roles=["下装", "鞋"],
        )
        self.assertEqual(outfits, [], "无 SKU 行回退且缺鞋→丢弃整套（原行为）")


if __name__ == "__main__":
    unittest.main()
