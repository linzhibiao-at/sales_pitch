"""Elasticsearch 可选客户端（仅请求审计落库/查询用）。

ES 不可用时所有方法静默降级（返回空/None），不影响话术主链路。
"""

from __future__ import annotations

import logging
from typing import Any

from backend.config import (
    create_elasticsearch_client,
    env_or_empty,
    get_elasticsearch_hosts,
    get_elasticsearch_indices,
    load_config,
)

logger = logging.getLogger(__name__)

try:
    from elasticsearch import Elasticsearch
except ImportError:  # pragma: no cover
    Elasticsearch = None  # type: ignore


class EsClient:
    def __init__(self) -> None:
        cfg = load_config()
        es = cfg.get("elasticsearch") or {}
        self._enabled = bool(es.get("enabled"))
        self._hosts = get_elasticsearch_hosts(cfg)
        self._indices = get_elasticsearch_indices(cfg)
        self._client: Any = None
        if self._enabled and Elasticsearch:
            user = env_or_empty(str(es.get("username_env") or ""))
            pwd = env_or_empty(str(es.get("password_env") or ""))
            try:
                self._client = create_elasticsearch_client(
                    self._hosts,
                    username=user,
                    password=pwd,
                    timeout_sec=30,
                )
                if not self._client.ping():
                    logger.warning("ES ping failed, disable ES")
                    self._client = None
            except Exception as e:  # noqa: BLE001
                logger.warning("ES init failed: %s", e)
                self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    def index_doc(
        self,
        index_key: str,
        doc: dict[str, Any],
        doc_id: str | None = None,
        refresh: bool = True,
    ) -> str | None:
        """单文档 index,返回 ES `_id`;不可用/失败/未知索引返回 None。"""
        if not self._client or not isinstance(doc, dict):
            return None
        if index_key not in self._indices:
            return None
        idx = self._indices[index_key]
        try:
            if doc_id:
                res = self._client.index(
                    index=idx, body=doc, id=doc_id, refresh=refresh,
                )
            else:
                res = self._client.index(index=idx, body=doc, refresh=refresh)
            return str(res.get("_id") or "")
        except Exception as e:  # noqa: BLE001
            logger.warning("es index_doc %s: %s", index_key, e)
            return None

    def search_docs(
        self,
        index_key: str,
        body: dict[str, Any],
    ) -> list[tuple[str, dict[str, Any]]]:
        """发出 search,返回 [(doc_id, _source), ...];不可用/未知索引返回 []。"""
        if not self._client:
            return []
        if index_key not in self._indices:
            return []
        idx = self._indices[index_key]
        try:
            res = self._client.search(index=idx, body=body)
            hits = res.get("hits", {}).get("hits", [])
            out: list[tuple[str, dict[str, Any]]] = []
            for h in hits:
                src = h.get("_source")
                if isinstance(src, dict):
                    out.append((str(h.get("_id") or ""), src))
            return out
        except Exception as e:  # noqa: BLE001
            logger.warning("es search_docs %s: %s", index_key, e)
            return []
