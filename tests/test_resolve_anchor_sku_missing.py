"""_resolve_anchor_sku 边界回归：输入 SKU 在索引里不存在时按"无 SKU"处理。

覆盖三个对外入口汇聚到的解析函数 `_resolve_anchor_sku`
(`backend/services/recommend_service.py`)：
- `RecommendSkusRequest.anchor_sku_id` 显式输入但索引未命中 → None
- `ChatRequest.selected_sku_id` 显式输入但索引未命中 → None
- message-token 命中/未命中、SPU 兜底、正常命中 SKU 各路径保持原行为。
"""

from __future__ import annotations

import unittest

from backend.models import ChatRequest, RecommendSkusRequest
from backend.services.recommend_service import RecommendService


_GOOD_SKU = "GOOD123"
_BAD_SKU = "NOPE999"


class _StubData:
    """最小 DataFacade stub：仅 get_sku，命中 _GOOD_SKU。"""

    def get_sku(self, sku_id: str):
        sid = (sku_id or "").strip()
        return {"sku_id": sid, "title": f"stub-{sid}"} if sid == _GOOD_SKU else None


class _StubSkuRetriever:
    """expand_spu 兜底：返回空，触发 None 分支。"""

    def expand_spu(self, spu_id: str, size: int = 200):
        return []


def _make_svc() -> RecommendService:
    # 跳过 __init__（避免连 ES），直接注入 stub 数据层。
    svc = RecommendService.__new__(RecommendService)
    svc._data = _StubData()
    svc._sku_r = _StubSkuRetriever()
    return svc


class ResolveAnchorSkuMissingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = _make_svc()

    def test_chat_selected_sku_missing_returns_none(self) -> None:
        req = ChatRequest(message="", selected_sku_id=_BAD_SKU)
        self.assertIsNone(self.svc._resolve_anchor_sku(req))

    def test_chat_selected_sku_present_returns_id(self) -> None:
        req = ChatRequest(message="", selected_sku_id=_GOOD_SKU)
        self.assertEqual(self.svc._resolve_anchor_sku(req), _GOOD_SKU)

    def test_recommend_anchor_sku_missing_returns_none(self) -> None:
        req = RecommendSkusRequest(anchor_sku_id=_BAD_SKU)
        self.assertIsNone(self.svc._resolve_anchor_sku(req))

    def test_recommend_anchor_sku_present_returns_id(self) -> None:
        req = RecommendSkusRequest(anchor_sku_id=_GOOD_SKU)
        self.assertEqual(self.svc._resolve_anchor_sku(req), _GOOD_SKU)

    def test_chat_empty_input_returns_none(self) -> None:
        req = ChatRequest(message="")
        self.assertIsNone(self.svc._resolve_anchor_sku(req))

    def test_message_token_missing_returns_none(self) -> None:
        # message 里塞一个非 SKU 货号 token，索引未命中 → None。
        req = ChatRequest(message=_BAD_SKU)
        self.assertIsNone(self.svc._resolve_anchor_sku(req))

    def test_message_token_present_returns_token(self) -> None:
        req = ChatRequest(message=_GOOD_SKU)
        self.assertEqual(self.svc._resolve_anchor_sku(req), _GOOD_SKU)


if __name__ == "__main__":
    unittest.main()
