"""离线 JSONL / JSON 本地数据存储（已停用）。

数据访问已统一走 ES（见 `DataFacade` + `EsClient`），本地文件不再加载。
本类保留为惰性空壳：`load()` 为 no-op，各集合恒为空，仅用于兼容旧构造签名
与 ES 不可用时的降级（返回空）。
"""

from __future__ import annotations

from typing import Any, Iterator


def _iter_jsonl(path: "Any") -> Iterator[dict[str, Any]]:
    return iter(())  # 已停用本地加载


class LocalDataStore:
    def __init__(self) -> None:
        self.skus: dict[str, dict[str, Any]] = {}
        self.outfits: dict[str, dict[str, Any]] = {}
        self.sku_to_outfits: dict[str, list[str]] = {}
        self.spu_to_skus: dict[str, list[str]] = {}
        self._loaded = True  # no-op：无本地文件需加载

    def load(self) -> None:
        """no-op：本地文件加载已停用，数据走 ES。"""
        return

    def search_skus_text(self, q: str, limit: int = 50) -> list[dict[str, Any]]:
        return []

    def search_outfits_text(
        self,
        q: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return []
