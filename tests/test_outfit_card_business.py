"""业务精简 outfit 出参测试。"""

from backend.services.card_builder import outfit_card_business, slim_outfit_cards


def test_outfit_card_business_keeps_only_required_fields():
    card = {
        "outfit_id": "guide_123",
        "name": "导购搭配",
        "price_total": 1260.0,
        "reason": "清爽运动风",
        "items": [
            {"sku_id": "SKU_A", "title": "上衣"},
            {"skuId": "SKU_B"},
            {"attrAlias": ""},
        ],
    }
    slim = outfit_card_business(card)
    assert slim == {
        "outfit_id": "guide_123",
        "sku_ids": ["SKU_A", "SKU_B"],
        "reason": "清爽运动风",
    }


def test_slim_outfit_cards_maps_list():
    cards = [
        {"outfit_id": "o1", "items": [{"sku_id": "A"}], "reason": "r1"},
        {"outfit_id": "o2", "items": [], "reason": ""},
    ]
    out = slim_outfit_cards(cards)
    assert len(out) == 2
    assert out[0]["sku_ids"] == ["A"]
    assert out[1]["reason"] == ""
