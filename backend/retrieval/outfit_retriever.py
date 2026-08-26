"""搭配召回（ES 优先 / 本地 preview fallback，不含 Milvus 搭配向量）。"""

from __future__ import annotations

from typing import Any, Dict, List

from backend.local_data_store import LocalDataStore
from backend.retrieval.data_facade import DataFacade


class OutfitRetriever:
    def __init__(
        self,
        store: LocalDataStore,
        data: DataFacade | None = None,
    ) -> None:
        self._data = data or DataFacade(store)

    def by_sku_membership(self, sku_id: str) -> List[Dict[str, Any]]:
        return self._data.outfits_by_sku(sku_id)

    def by_text(
        self,
        q: str,
        limit: int = 30,
        *,
        trace_id: str | None = None,
    ) -> List[Dict[str, Any]]:
        from backend.api_debug import log_text_search_recall_io

        rows = self._data.search_outfits_text(q, limit)
        oids = [str(r.get("outfit_id") or "") for r in rows if r.get("outfit_id")]
        log_text_search_recall_io(
            trace_id=trace_id,
            entity="outfit",
            channel="data_facade",
            query=q,
            limit=limit,
            output_ids=oids,
            extra={"source": "es_or_preview"},
        )
        return rows
