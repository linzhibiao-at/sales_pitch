"""anchor_graph 通路「鞋类空 role → 排序错乱」回归测试。

bug：只输入图片时搭配主体走 anchor_graph（固定搭配库 / micro_guide 源）。
该源 outfit item 的 role 可能为空——鞋类既非上装也非下装，建库时 upDown
兜底为空。排序函数 ``order_outfit_items_by_role`` 只读 ``it.get("role")``，
空 role 经 ``role_display_priority`` 返回 99 垫底，导致鞋类被排到配饰之后，
最终顺序形如 top → bottoms → accessory → shoes，而非预期的
top → bottoms → shoes → accessory。

文本输入走 synth 路径，item 来自 skus 索引行（role 正确），故不复现——
本 bug 仅在 image-only（anchor_graph）路径暴露。

复现：库内搭配 [top(上装), bottoms(下装), shoes(role=""), accessory(配饰)]，
store.get_sku("SH1") 返回 role="鞋" 供回退。fix 前鞋排末尾，fix 后鞋排第三。
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


def _shoes_item_empty_role() -> dict:
    """固定搭配库（micro_guide 源）里的鞋类 item：role 为空（upDown 兜底）。"""
    return {
        "sku_id": "SH1",
        "spu_id": "SSH1",
        "role": "",  # 鞋类既非上装也非下装，建库时 upDown 兜底为空
        "category_l2": "休闲鞋",
        "title": "女士休闲鞋",
        "gender": "女",
        "scene_domain": "daily",
    }


def _accessory() -> dict:
    return {
        "sku_id": "AC1",
        "spu_id": "SAC1",
        "role": "配饰",
        "category_l2": "包",
        "title": "女士斜挎包",
        "gender": "女",
        "scene_domain": "daily",
    }


class _FakeStore:
    """模拟 DataFacade：outfits_by_skus_batch 返回库内搭配；get_sku 返回
    带正确 role 的 SKU 行（供 _resolve_item_role 回退）。"""

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


class ShoeEmptyRoleOrderTest(unittest.TestCase):
    """鞋类 role 为空时，排序应为 top → bottoms → shoes → accessory。"""

    def _store(self) -> _FakeStore:
        anchor = _anchor_top()
        bottoms = _bottoms()
        shoes_item = _shoes_item_empty_role()
        accessory = _accessory()
        # SKU 行带正确 role，供 _resolve_item_role 回退
        skus = {
            "A1": anchor,
            "P1": bottoms,
            "SH1": {**shoes_item, "role": "鞋"},  # 库里鞋的 role 正确
            "AC1": accessory,
        }
        # 故意把库内搭配的 item 顺序打乱，避免碰巧撞对
        outfit = _outfit(
            "O1", [accessory, shoes_item, bottoms, anchor]
        )
        return _FakeStore(
            skus=skus,
            by_sku={"A1": [outfit]},
        )

    def test_shoe_sorted_before_accessory(self):
        store = self._store()
        outfits, _ = recall_anchor_graph_outfits(store, "A1")
        self.assertEqual(len(outfits), 1, "应返回 1 套搭配")
        items = outfits[0].get("items") or []
        roles = [it.get("role") for it in items if isinstance(it, dict)]
        # 归一化到英文 token 再比较顺序
        from backend.intent.slot_defs import normalize_role

        order = [normalize_role(r) for r in roles]
        self.assertIn("top", order)
        self.assertIn("bottoms", order)
        self.assertIn("shoes", order)
        # 鞋必须排在配饰之前（fix 前鞋被当未知角色 priority 99 垫底，排到配饰后）
        if "accessory" in order:
            self.assertLess(
                order.index("shoes"),
                order.index("accessory"),
                f"鞋应排在配饰之前，实际顺序: {order}",
            )
        # top 在 bottoms 之前、bottoms 在 shoes 之前
        self.assertLess(order.index("top"), order.index("bottoms"))
        self.assertLess(order.index("bottoms"), order.index("shoes"))


if __name__ == "__main__":
    unittest.main()
