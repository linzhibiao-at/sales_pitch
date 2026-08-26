"""anchor_graph 通路「重复 anchor」回归测试。

bug：当固定搭配库的某套搭配**已包含 anchor SKU 本身**，且同时含一个
Milvus 召回的近邻 SKU（同款不同色等）时，近邻→anchor 的替换会把近邻也
改成 anchor，导致一套搭配里出现两件 sku_id == anchor 的单品。两件随后
都被 anchor-first 逻辑标上 is_anchor=True，target_role 剪枝无法去除，最终
线上召回的 synth_graph_* 搭配里同一货号出现两次。

复现：输入 F11W628505FDB（top），库内搭配 [F11W628505FDB(top), F11W628501FWT(top),
F11W628610FDB(bottoms), F12W621119FSN(shoes)]，candidate_skus 含近邻
F11W628501FWT → 错误替换出第二个 anchor。
"""

from __future__ import annotations

import unittest

from backend.services.outfit_recall import recall_anchor_graph_outfits


def _anchor_top() -> dict:
    return {
        "sku_id": "A1",
        "spu_id": "SA1",
        "role": "上装",
        "category_l2": "针织上衣",
        "title": "女士基础针织上衣",
        "gender": "女",
        "scene_domain": "daily",
    }


def _neighbor_top() -> dict:
    """与 anchor 同 SPU 不同色的近邻上装（Milvus 图向量召回）。"""
    return {
        "sku_id": "N1",
        "spu_id": "SA1",  # 同 SPU，色号不同
        "role": "上装",
        "category_l2": "针织上衣",
        "title": "女士基础针织上衣(近邻色)",
        "gender": "女",
        "scene_domain": "daily",
    }


def _bottoms() -> dict:
    return {
        "sku_id": "P1",
        "spu_id": "SP1",
        "role": "下装",
        "category_l2": "针织长裤",
        "title": "女士舒适针织长裤",
        "gender": "女",
        "scene_domain": "daily",
    }


def _shoes() -> dict:
    return {
        "sku_id": "SH1",
        "spu_id": "SSH1",
        "role": "鞋",
        "category_l2": "休闲鞋",
        "title": "女士休闲鞋",
        "gender": "女",
        "scene_domain": "daily",
    }


class _FakeStore:
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


class DuplicateAnchorTest(unittest.TestCase):
    """anchor 已在搭配中时，不应再将近邻替换为 anchor 造成重复。"""

    def _store(self) -> _FakeStore:
        anchor = _anchor_top()
        neighbor = _neighbor_top()
        # 库内搭配同时含 anchor(A1) 与近邻(N1)
        outfit = _outfit("O1", [anchor, neighbor, _bottoms(), _shoes()])
        return _FakeStore(
            skus={"A1": anchor, "N1": neighbor},
            by_sku={"A1": [outfit], "N1": [outfit]},
        )

    def _anchor_count(self, outfit: dict) -> int:
        return sum(
            1 for it in outfit.get("items", [])
            if isinstance(it, dict) and it.get("sku_id") == "A1"
        )

    def test_no_duplicate_anchor_with_target_roles(self):
        """target=[下装,鞋]：anchor 已在搭配中→不替换近邻，近邻上装被剪枝，仅 1 个 anchor。"""
        store = self._store()
        outfits, _ = recall_anchor_graph_outfits(
            store,
            "A1",
            candidate_skus=["N1"],
            intent_target_roles=["下装", "鞋"],
        )
        self.assertEqual(len(outfits), 1, "整套应保留（anchor+下装+鞋）")
        kept = outfits[0]
        self.assertEqual(self._anchor_count(kept), 1, "不应出现两件 anchor")
        # 近邻上装被 target_role 剪枝
        roles = {
            it.get("role") for it in kept.get("items", [])
            if isinstance(it, dict) and not (it.get("is_anchor") or it.get("is_master"))
        }
        self.assertNotIn("上装", roles, "近邻上装应被剪枝")

    def test_no_duplicate_anchor_without_target_roles(self):
        """无 target_roles：anchor 已在搭配中→不替换近邻。返回的任何搭配都不应
        出现两件 anchor（旧逻辑会保留一套含重复 anchor 的搭配；fix 后多余近邻
        交给冲突规则，不再被伪造成第二件 anchor）。"""
        store = self._store()
        outfits, _ = recall_anchor_graph_outfits(
            store,
            "A1",
            candidate_skus=["N1"],
            intent_target_roles=None,
        )
        for kept in outfits:
            self.assertEqual(
                self._anchor_count(kept), 1, "不应出现两件 anchor"
            )


if __name__ == "__main__":
    unittest.main()
