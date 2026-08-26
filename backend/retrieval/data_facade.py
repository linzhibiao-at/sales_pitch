"""统一数据访问层：运行时全走 ES（套装/SKU 数据均在 ES 索引中）。

不再加载本地 JSON 文件。ES 不可用时各方法降级返回空结果。
"""

from __future__ import annotations

from typing import Any

from backend.local_data_store import LocalDataStore
from backend.retrieval.es_client import EsClient, OPERATIONAL_OUTFIT_SOURCES
from backend.retrieval.outfit_color_series import (
    outfit_color_series_tags,
    outfit_has_color_series,
    sort_color_series_counts,
)

_DEFAULT_ES = object()
# 运营搭配来源（运营 catalog 可见），合成搭配(synth_*)不在此列。


class DataFacade:
    def __init__(
        self,
        store: LocalDataStore | None = None,
        es: EsClient | None | object = _DEFAULT_ES,
    ) -> None:
        # store 保留为惰性空壳（LocalDataStore 已不再加载本地文件），仅用于
        # 兼容旧构造签名；数据访问全部走 self._es。
        self._store = store or LocalDataStore()
        self._es = EsClient() if es is _DEFAULT_ES else es

    @property
    def _es_ok(self) -> bool:
        return bool(self._es and self._es.available)

    def get_sku(self, sku_id: str) -> dict[str, Any] | None:
        sid = (sku_id or "").strip()
        if not sid or not self._es_ok:
            return None
        return self._es.get_doc("skus", sid)

    def get_skus(self, sku_ids: list[str]) -> list[dict[str, Any]]:
        ids = [str(x).strip() for x in sku_ids if str(x).strip()]
        if not ids or not self._es_ok:
            return []
        return self._es.mget_docs("skus", ids)

    def get_outfit(self, outfit_id: str) -> dict[str, Any] | None:
        oid = (outfit_id or "").strip()
        if not oid or not self._es_ok:
            return None
        return self._es.get_doc("outfits", oid)

    def bulk_upsert_outfits(
        self,
        docs: list[tuple[str, dict[str, Any]]],
    ) -> tuple[int, int]:
        """按 _id 批量 upsert outfits 文档（合成搭配持久化用）。

        返回 (success, error)。ES 不可用或空入参时静默降级。
        """
        if not self._es_ok or not docs:
            return 0, len(docs)
        return self._es.bulk_upsert_docs("outfits", docs)

    def mget_outfits(self, outfit_ids: list[str]) -> list[dict[str, Any]]:
        ids = [str(x).strip() for x in outfit_ids if str(x).strip()]
        if not ids or not self._es_ok:
            return []
        rows = self._es.mget_docs("outfits", ids)
        by_id = {str(r.get("outfit_id") or r.get("idMatch") or ""): r for r in rows}
        return [by_id[oid] for oid in ids if oid in by_id]

    def outfits_by_sku(
        self,
        sku_id: str,
        size: int = 100,
        *,
        sources: list[str] | tuple[str, ...] | None = OPERATIONAL_OUTFIT_SOURCES,
    ) -> list[dict[str, Any]]:
        sid = (sku_id or "").strip()
        if not sid or not self._es_ok:
            return []
        source_filter = [str(x).strip() for x in (sources or []) if str(x).strip()]
        return self._es.search_outfits_by_sku(sid, size=size, sources=source_filter)

    def outfits_by_skus_batch(
        self,
        sku_ids: list[str],
        size: int = 200,
        *,
        sources: list[str] | tuple[str, ...] | None = OPERATIONAL_OUTFIT_SOURCES,
    ) -> list[dict[str, Any]]:
        """Batch lookup outfits containing ANY of the given SKU IDs."""
        ids = [str(x).strip() for x in sku_ids if str(x).strip()]
        if not ids or not self._es_ok:
            return []
        source_filter = [str(x).strip() for x in (sources or []) if str(x).strip()]
        return self._es.search_outfits_by_skus_batch(
            ids, size=size, sources=source_filter,
        )

    def expand_spu(self, spu_id: str, size: int = 200) -> list[str]:
        """spu_id -> [sku_id, ...]（走 ES skus 索引 by-spu 反查）。"""
        sid = (spu_id or "").strip()
        if not sid or not self._es_ok:
            return []
        return self._es.expand_spu(sid, size=size)

    def companion_skus_by_anchor(
        self,
        anchor_sku_id: str,
        target_roles: list[str] | None = None,
        *,
        size: int = 100,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        roles = {str(r).strip() for r in (target_roles or []) if str(r).strip()}
        anchor = (anchor_sku_id or "").strip()
        if not anchor:
            return [], []
        rows: list[dict[str, Any]] = []
        outfit_ids: list[str] = []
        seen: set[str] = set()
        for outfit in self.outfits_by_sku(anchor, size=size):
            oid = str(outfit.get("outfit_id") or outfit.get("idMatch") or "")
            if oid:
                outfit_ids.append(oid)
            for item in outfit.get("items") or []:
                if not isinstance(item, dict):
                    continue
                sid = self._item_sku_id(item)
                if not sid or sid == anchor or sid in seen:
                    continue
                role = str(item.get("role") or "").strip()
                if roles and role and role not in roles:
                    continue
                row = self.get_sku(sid) or self._sku_from_item(item)
                if not row:
                    continue
                item_role = str(row.get("role") or role or "").strip()
                if roles and item_role not in roles:
                    continue
                out = dict(row)
                out.setdefault("role", item_role)
                rows.append(out)
                seen.add(sid)
        return rows, list(dict.fromkeys(outfit_ids))

    def search_skus_text(self, query: str, limit: int = 30) -> list[dict[str, Any]]:
        if not self._es_ok:
            return []
        ids = self._es.search_skus(query, None, limit)
        if not ids:
            return []
        return self.get_skus(ids)

    def search_outfits_text(self, query: str, limit: int = 30) -> list[dict[str, Any]]:
        if not self._es_ok:
            return []
        return self._es.search_outfits(query, limit)

    def outfit_source_counts(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = self._es.outfit_source_counts() if self._es_ok else []
        return self._sort_source_counts(rows)

    @staticmethod
    def _sort_source_counts(
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        order = {
            source: idx
            for idx, source in enumerate(OPERATIONAL_OUTFIT_SOURCES)
        }
        return sorted(
            rows,
            key=lambda row: (
                order.get(str(row.get("source") or ""), 999),
                str(row.get("source") or ""),
            ),
        )

    def outfit_color_series_counts(
        self,
        *,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        source_key = (source or "").strip() or None
        if not self._es_ok:
            return []
        rows = self._es.outfit_color_series_counts(source=source_key)
        return sort_color_series_counts(rows)

    def outfit_season_counts(
        self,
        *,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        source_key = (source or "").strip() or None
        if not self._es_ok:
            return []
        rows = self._es.outfit_season_counts(source=source_key)
        return self._sort_season_counts(rows)

    @staticmethod
    def _sort_season_counts(
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        order = {"春": 0, "夏": 1, "秋": 2, "冬": 3}
        return sorted(
            rows,
            key=lambda row: (
                order.get(str(row.get("season") or ""), 999),
                str(row.get("season") or ""),
            ),
        )

    def browse_outfits(
        self,
        offset: int = 0,
        size: int = 80,
        *,
        source: str | None = None,
        color_series: str | None = None,
        season: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        source_key = (source or "").strip() or None
        color_key = (color_series or "").strip() or None
        season_key = (season or "").strip() or None
        if not self._es_ok:
            return [], 0
        rows, total = self._es.browse_outfits(
            offset,
            size,
            source=source_key,
            color_series=color_key,
            season=season_key,
        )
        return self._enrich_outfit_color_tags(rows), total

    def browse_skus(
        self,
        offset: int = 0,
        size: int = 60,
        *,
        gender: list[str] | None = None,
        age: list[str] | None = None,
        season: list[str] | None = None,
        color_series: list[str] | None = None,
        category_l2: list[str] | None = None,
        series: list[str] | None = None,
        role: list[str] | None = None,
        up_time_since: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """按结构化筛选分页浏览 SKU（镜像 ``browse_outfits``）。"""
        if not self._es_ok:
            return [], 0
        return self._es.browse_skus(
            offset,
            size,
            gender=gender,
            age=age,
            season=season,
            color_series=color_series,
            category_l2=category_l2,
            series=series,
            role=role,
            up_time_since=up_time_since,
        )

    def sku_facets(self) -> dict[str, list[dict[str, Any]]]:
        """SKU 索引的数据驱动分面（类目/系列）。"""
        if not self._es_ok:
            return {}
        return self._es.sku_facets()


    @staticmethod
    def _enrich_outfit_color_tags(
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in rows:
            tags = outfit_color_series_tags(row)
            if tags and not row.get("color_series_tags"):
                enriched = dict(row)
                enriched["color_series_tags"] = tags
                out.append(enriched)
            else:
                out.append(row)
        return out

    @staticmethod
    def _item_sku_id(item: dict[str, Any]) -> str:
        raw = (
            item.get("sku_id")
            or item.get("skuId")
            or item.get("attrAlias")
            or item.get("idAlias")
        )
        return str(raw).strip() if raw is not None else ""

    @staticmethod
    def _outfit_source(outfit: dict[str, Any]) -> str:
        source = str(outfit.get("source") or "").strip()
        if source:
            return source
        shop = str(outfit.get("shopName") or "").strip()
        if "微导购" in shop:
            return "micro_guide"
        return "cc_material"

    @staticmethod
    def _sku_from_item(item: dict[str, Any]) -> dict[str, Any]:
        sid = DataFacade._item_sku_id(item)
        if not sid:
            return {}
        return {
            "sku_id": sid,
            "spu_id": str(item.get("spu_id") or item.get("idAlias") or ""),
            "title": item.get("title"),
            "role": item.get("role"),
            "price": item.get("price"),
            "display_image": DataFacade._item_image(item),
            "tryon_image": item.get("tryon_image") or DataFacade._item_image(item),
        }

    @staticmethod
    def _item_image(item: dict[str, Any]) -> str:
        for key in ("display_image", "tryon_image"):
            val = str(item.get(key) or "").strip()
            if val:
                return val
        # index_images 数组取第一个非空值
        idx_imgs = item.get("index_images")
        if isinstance(idx_imgs, list):
            for u in idx_imgs:
                v = str(u or "").strip()
                if v:
                    return v
        images = item.get("images") or {}
        if isinstance(images, dict):
            val = str(images.get("cover") or "").strip()
            if val:
                return val
        return ""
