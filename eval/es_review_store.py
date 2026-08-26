"""评审意见 ES 存储(硬切换 storage=es 时启用)。

复用 backend.retrieval.es_client.EsClient 的通用单文档方法;评审专属
query 在本类构造。文档 _id 由 ES 自动生成,作为返回给前端的 id。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from eval.review_store import ReviewStore

logger = logging.getLogger(__name__)

_GET_SIZE = 10000  # 单批次评审量上限(超出会静默截断,后续可改 scroll)


class EsReviewStore:
    def __init__(self, es: Optional[Any] = None) -> None:
        if es is not None:
            self._es = es
        else:
            from backend.retrieval.es_client import EsClient
            self._es = EsClient()
        if not getattr(self._es, "available", False):
            logger.warning("EsReviewStore: ES 不可用(storage=es 下评审端点将 503)")

    def add(
        self, *,
        data_file: str,
        input_sku_id: str,
        outfit_id: str,
        rating: Optional[int] = None,
        comment: Optional[str] = None,
        reviewer: Optional[str] = None,
        reviewer_role: Optional[str] = None,
        reviewer_name: Optional[str] = None,
    ) -> dict:
        if not getattr(self._es, "available", False):
            raise RuntimeError("评审存储不可用(ES)")
        now = datetime.now(timezone.utc).isoformat()
        doc = {
            "data_file": data_file,
            "input_sku_id": input_sku_id,
            "outfit_id": outfit_id,
            "rating": rating,
            "comment": comment,
            "reviewer": reviewer,
            "reviewer_role": reviewer_role,
            "reviewer_name": reviewer_name,
            "created_at": now,
            "updated_at": now,
        }
        doc_id = self._es.index_doc("reviews", doc)
        if not doc_id:
            raise RuntimeError("评审写入 ES 失败")
        out = dict(doc)
        out["id"] = doc_id
        return out

    def get(self, data_file: str) -> list[dict]:
        if not getattr(self._es, "available", False):
            raise RuntimeError("评审存储不可用(ES)")
        body = {
            "size": _GET_SIZE,
            "query": {"term": {"data_file": data_file}},
            "sort": [{"created_at": {"order": "desc"}}],
        }
        rows = self._es.search_docs("reviews", body)
        if len(rows) >= _GET_SIZE:
            logger.warning(
                "EsReviewStore.get 截断: data_file=%s 命中上限 %d",
                data_file, _GET_SIZE,
            )
        out: list[dict] = []
        for doc_id, src in rows:
            row = dict(src)
            row["id"] = doc_id
            out.append(row)
        return out

    def delete(self, id: str) -> bool:
        if not getattr(self._es, "available", False):
            raise RuntimeError("评审存储不可用(ES)")
        return bool(self._es.delete_doc("reviews", str(id)))
