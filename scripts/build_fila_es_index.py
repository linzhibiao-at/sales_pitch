#!/usr/bin/env python3
"""FILA fila_agent：从统一 ETL 产物构建 Elasticsearch 索引。

与 backend/retrieval/es_client.py 对齐：
  - skus：`match` 字段 `search_text`，过滤字段 `gender`（keyword）

写入配置中的 skus、outfits 索引。

用法（在 fila_agent 目录）::

  source .venv/bin/activate
  pip3 install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
  export ES_HOSTS=http://localhost:9200   # 可选，默认读 config.yaml
  python3 scripts/build_fila_es_index.py [--reset] [--incremental] [--batch-size 400]

更新策略：
  - **全量**：默认每次按 JSONL 全量 ``index``（同 _id 覆盖）。``--reset`` 先删索引再建映射并写入。
  - **增量**：``--incremental`` 仅当文档内容哈希相对
    ``data/logs/fila_index_sync_state.json`` 变化时才 bulk，减少写入。
  - **孤立文档**：``--prune-orphans`` 删除「上次状态中有、当前 JSONL 已不存在」的 _id
    （适用于源数据中删除了 SKU/搭配）。

远程集群若需账号::

  export ES_USERNAME=...
  export ES_PASSWORD=...

启用在线检索前请在 config.yaml 设置 elasticsearch.enabled: true。

``outfits`` 文档来自 ``data/preview/fila_outfits.json``，含 ``sku_ids`` /
``spu_ids``（由 ``items`` 抽取）和标准化后的 ``items`` 明细。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Iterator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
PROCESSED = ROOT / "data" / "processed"
PREVIEW_OUTFITS = ROOT / "data" / "preview" / "fila_outfits.json"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import (
    create_elasticsearch_client,
    env_or_empty,
    get_elasticsearch_hosts,
    get_elasticsearch_indices,
    load_config,
)
from backend.empty_image_urls import sku_has_empty_tryon_image
from backend.intent.color_series_mapper import map_color_to_series_list
from backend.retrieval.outfit_color_series import outfit_color_series_tags
from scripts.hybrid_text import build_keyword_text

from index_sync_state import (
    DEFAULT_STATE_PATH,
    clear_es_bucket,
    doc_hash,
    load_state,
    save_state,
)

try:
    from elasticsearch import Elasticsearch
    from elasticsearch.helpers import bulk
except ImportError:
    Elasticsearch = None  # type: ignore
    bulk = None  # type: ignore


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def connect_es(cfg: dict[str, Any]) -> Any:
    if Elasticsearch is None:
        logger.error("请安装: pip install 'elasticsearch>=7,<8'")
        raise SystemExit(1)
    es_cfg = cfg.get("elasticsearch") or {}
    hosts = get_elasticsearch_hosts(cfg)
    user_env = str(es_cfg.get("username_env") or "ES_USERNAME")
    pwd_env = str(es_cfg.get("password_env") or "ES_PASSWORD")
    user = env_or_empty(user_env)
    pwd = env_or_empty(pwd_env)
    client = create_elasticsearch_client(
        hosts,
        username=user,
        password=pwd,
        timeout_sec=60,
    )
    if not client.ping():
        logger.error("无法连接 Elasticsearch: %s", hosts)
        raise SystemExit(1)
    logger.info("已连接 ES: %s", hosts)
    return client


def delete_if_reset(client: Any, name: str, reset: bool) -> None:
    if not reset:
        return
    if client.indices.exists(index=name):
        client.indices.delete(index=name)
        logger.info("已删除索引: %s", name)


def create_index(client: Any, name: str, body: dict[str, Any]) -> None:
    """使用 elasticsearch-py 7.x 兼容的 body 参数创建索引。"""
    client.indices.create(index=name, body=body)


def skus_mapping() -> dict[str, Any]:
    """skus 索引 mapping（纯函数，供 create_skus_index 与单测共用）。"""
    # IK 中文分词：索引时最细粒度切分(召回更全)，查询时智能切分(更精准)
    ik_index = {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"}
    return {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
        },
        "mappings": {
            "properties": {
                "sku_id": {"type": "keyword"},
                "spu_id": {"type": "keyword"},
                "search_text": ik_index,
                "search_keywords": ik_index,
                "title": ik_index,
                "gender": {"type": "keyword"},
                "age": {"type": "keyword"},
                "up_time": {
                    "type": "date",
                    "format": "yyyy-MM-dd HH:mm:ss||yyyy-MM-dd",
                },
                "role": {"type": "keyword"},
                "brand": {"type": "keyword"},
                "group_brand": {"type": "keyword"},
                "series": {"type": "keyword"},
                "season": {"type": "keyword"},
                "price": {"type": "double"},
                "color_name": {"type": "keyword"},
                "color_series": {"type": "keyword"},
                "color_series_count": {"type": "integer"},
                "category_l1": {"type": "keyword"},
                "category_l2": {"type": "keyword"},
                "category_l3": {"type": "keyword"},
                "length_class": {"type": "keyword"},
                "layer": {"type": "keyword"},
                "coverage": {"type": "keyword"},
                "is_intimate": {"type": "boolean"},
                "scene_domain": {"type": "keyword"},
                "modeling": {"type": "keyword"},
                "up_down_raw": {"type": "keyword"},
                "occasion_tags": {"type": "keyword"},
                "style_tags": {"type": "keyword"},
                "display_image": {"type": "keyword"},
                "index_images": {"type": "keyword"},
                "tryon_image": {"type": "keyword"},
                "all_images": {
                    "type": "nested",
                    "properties": {
                        "path": {"type": "keyword"},
                        "id_pa": {"type": "keyword"},
                        "order_id": {"type": "integer"},
                        "image_type": {"type": "keyword"},
                    },
                },
                "ai_select": {
                    "type": "object",
                    "enabled": True,
                    "properties": {
                        "path": {"type": "keyword"},
                        "note": ik_index,
                        "candidate_count": {"type": "keyword"},
                        "chosen_id_pa": {"type": "keyword"},
                        "chosen_order_id": {"type": "keyword"},
                        "chosen_image_type": {"type": "keyword"},
                    },
                },
                "image_quality": {"type": "object", "enabled": True},
                "material": {"type": "keyword"},
                "sub_series": {"type": "keyword"},
                "color_family": {"type": "keyword"},
                "id_goods": {"type": "keyword"},
                "id_pa": {"type": "keyword"},
                # ── descent 复刻新增字段（catalog build_sku_record 补齐）──
                "product_name_short": ik_index,
                "goods_sn": {"type": "keyword"},
                "brand_line": {"type": "keyword"},
                "category": {"type": "keyword"},
                "length": {"type": "keyword"},
                "year": {"type": "keyword"},
                "technology": ik_index,
                "features": ik_index,
                "selling_point_label": ik_index,
                "keyword": ik_index,
                "market_price": {"type": "double"},
                "min_price": {"type": "double"},
                "max_price": {"type": "double"},
                "onsell": {"type": "integer"},
                "sales": {"type": "integer"},
                "sales_week": {"type": "integer"},
                "sales_month": {"type": "integer"},
                "w_order": {"type": "integer"},
                "sku_count": {"type": "integer"},
                "color_images": {"type": "keyword"},
                "video_url": {"type": "keyword"},
            },
        },
    }


def create_skus_index(client: Any, name: str) -> None:
    if client.indices.exists(index=name):
        logger.info("索引已存在，跳过创建: %s", name)
        return
    create_index(client, name, skus_mapping())
    logger.info("已创建索引: %s", name)


def create_outfits_index(client: Any, name: str) -> None:
    if client.indices.exists(index=name):
        logger.info("索引已存在，跳过创建: %s", name)
        return
    # IK 中文分词
    ik_index = {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"}
    body = {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
        },
        "mappings": {
            "properties": {
                "outfit_id": {"type": "keyword"},
                "name": ik_index,
                "search_text": ik_index,
                "gender": {"type": "keyword"},
                "source": {"type": "keyword"},
                "season": {"type": "keyword"},
                "series_tags": {"type": "keyword"},
                "color_series_tags": {"type": "keyword"},
                "occasion_tags": {"type": "keyword"},
                "style_tags": {"type": "keyword"},
                "roles": {"type": "keyword"},
                "price_total": {"type": "double"},
                "status": {"type": "integer"},
                "display_image": {"type": "keyword"},
                "index_images": {"type": "keyword"},
                "background_img": {"type": "keyword"},
                "outfit_tryon_image": {"type": "keyword"},
                "master_sku_id": {"type": "keyword"},
                "master_spu_id": {"type": "keyword"},
                "sku_ids": {"type": "keyword"},
                "spu_ids": {"type": "keyword"},
                "items": {
                    "type": "nested",
                    "properties": {
                        "sku_id": {"type": "keyword"},
                        "spu_id": {"type": "keyword"},
                        "role": {"type": "keyword"},
                        "title": ik_index,
                        "price": {"type": "double"},
                        "display_image": {"type": "keyword"},
                        "tryon_image": {"type": "keyword"},
                        "is_master": {"type": "boolean"},
                    },
                },
            },
        },
    }
    create_index(client, name, body)
    logger.info("已创建索引: %s", name)


def _normalize_all_images(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            path = item.strip()
            if path:
                out.append({
                    "path": path,
                    "id_pa": "",
                    "order_id": 0,
                    "image_type": "",
                })
            continue
        if not isinstance(item, dict):
            continue
        path = str(
            item.get("path")
            or item.get("url")
            or item.get("image_url")
            or "",
        ).strip()
        if not path:
            continue
        try:
            order_id = int(float(item.get("order_id") or item.get("orderId") or 0))
        except (TypeError, ValueError):
            order_id = 0
        out.append({
            "path": path,
            "id_pa": str(item.get("id_pa") or item.get("idPa") or ""),
            "order_id": order_id,
            "image_type": str(
                item.get("image_type") or item.get("imageType") or "",
            ),
        })
    return out


def _normalize_ai_select(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    path = str(raw.get("path") or "").strip()
    if not path:
        return None
    return {
        "path": path,
        "note": str(raw.get("note") or ""),
        "candidate_count": str(
            raw.get("candidate_count") or raw.get("candidateCount") or "",
        ),
        "chosen_id_pa": str(
            raw.get("chosen_id_pa") or raw.get("chosenIdPa") or "",
        ),
        "chosen_order_id": str(
            raw.get("chosen_order_id") or raw.get("chosenOrderId") or "",
        ),
        "chosen_image_type": str(
            raw.get("chosen_image_type")
            or raw.get("chosenImageType")
            or "ai_select",
        ),
    }


def _normalize_index_images(raw: object) -> list[str]:
    """将 index_images 字段标准化为 URL 字符串列表。"""
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(u).strip() for u in parsed if str(u).strip()]
        except (ValueError, TypeError):
            pass
        return [raw] if raw else []
    if isinstance(raw, list):
        return [str(u).strip() for u in raw if str(u).strip()]
    return []


def sku_doc(row: dict[str, Any]) -> dict[str, Any]:
    season = row.get("season") or []
    if isinstance(season, str):
        season = [season]
    occ = row.get("occasion_tags") or []
    sty = row.get("style_tags") or []
    cs_list = map_color_to_series_list(
        str(row.get("attr_name") or row.get("color_name") or ""),
    )
    doc: dict[str, Any] = {
        "sku_id": str(row.get("sku_id") or ""),
        "spu_id": str(row.get("spu_id") or ""),
        "search_text": build_keyword_text(row),
        "search_keywords": str(
            row.get("search_keywords") or row.get("search_text") or "",
        ),
        "title": str(row.get("title") or ""),
        "gender": (
            [str(x) for x in row["gender"]]
            if isinstance(row.get("gender"), list)
            else ([str(row.get("gender"))] if row.get("gender") else [])
        ),
        "age": str(row.get("age") or ""),
        "role": str(row.get("role") or ""),
        "brand": str(row.get("brand") or ""),
        "group_brand": str(row.get("group_brand") or ""),
        "series": str(row.get("series") or ""),
        "season": [str(x) for x in season],
        "price": float(row.get("price") or 0.0),
        "color_name": str(row.get("color_name") or ""),
        "color_series": cs_list,
        "color_series_count": len(cs_list),
        "category_l1": str(row.get("category_l1") or ""),
        "category_l2": str(row.get("category_l2") or ""),
        "category_l3": str(row.get("category_l3") or ""),
        "up_down_raw": str(row.get("up_down_raw") or ""),
        "occasion_tags": [str(x) for x in occ],
        "style_tags": [str(x) for x in sty],
        "display_image": str(row.get("display_image") or ""),
        "index_images": _normalize_index_images(row.get("index_images")),
        "tryon_image": str(row.get("tryon_image") or ""),
        "all_images": _normalize_all_images(row.get("all_images")),
        "image_quality": row.get("image_quality") or {},
        "material": str(row.get("material") or ""),
        "sub_series": str(row.get("sub_series") or ""),
        "color_family": str(row.get("color_family") or ""),
        "length_class": str(row.get("length_class") or ""),
        "layer": str(row.get("layer") or ""),
        "coverage": str(row.get("coverage") or ""),
        "is_intimate": bool(row.get("is_intimate") or False),
        "scene_domain": str(row.get("scene_domain") or ""),
        "modeling": str(row.get("modeling") or ""),
        "id_goods": str(row.get("goods_id") or row.get("id_goods") or ""),
        "id_pa": str(row.get("id_pa") or ""),
        # ── descent 复刻新增字段（catalog build_sku_record 补齐）──
        "product_name_short": str(row.get("product_name_short") or ""),
        "goods_sn": str(row.get("goods_sn") or ""),
        "brand_line": str(row.get("brand_line") or ""),
        "category": str(row.get("category") or ""),
        "length": str(row.get("length") or ""),
        "year": str(row.get("year") or ""),
        "technology": str(row.get("technology") or ""),
        "features": str(row.get("features") or ""),
        "selling_point_label": str(row.get("selling_point_label") or ""),
        "keyword": str(row.get("keyword") or ""),
        "market_price": float(row.get("market_price") or 0.0),
        "min_price": float(row.get("min_price") or 0.0),
        "max_price": float(row.get("max_price") or 0.0),
        "onsell": int(row.get("onsell") or 0),
        "sales": int(row.get("sales") or 0),
        "sales_week": int(row.get("sales_week") or 0),
        "sales_month": int(row.get("sales_month") or 0),
        "w_order": int(row.get("w_order") or 0),
        "sku_count": int(row.get("sku_count") or 0),
        "color_images": str(row.get("color_images") or ""),
        "video_url": str(row.get("video_url") or ""),
    }
    # date 字段不可写空串：仅在 up_time 非空时写入，供 sort/范围过滤倒排。
    _up_time = str(row.get("up_time") or "").strip()
    if _up_time:
        doc["up_time"] = _up_time
    ai = _normalize_ai_select(row.get("ai_select") or row.get("aiSelect"))
    if ai:
        doc["ai_select"] = ai
    return doc


def _item_sku_id(item: dict[str, Any]) -> str:
    raw = (
        item.get("sku_id")
        or item.get("skuId")
        or item.get("attrAlias")
        or item.get("idAlias")
    )
    return str(raw).strip() if raw is not None else ""


def _item_spu_id(item: dict[str, Any]) -> str:
    raw = item.get("spu_id") or item.get("spuId") or item.get("idAlias")
    return str(raw).strip() if raw is not None else ""


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
    if not isinstance(images, dict):
        return ""
    cover = str(images.get("cover") or "").strip()
    if cover:
        return cover
    for key in ("outfitCd", "outfitCps"):
        vals = images.get(key) or []
        if isinstance(vals, list):
            for val in vals:
                url = str(val or "").strip()
                if url:
                    return url
    return ""


def _item_role(item: dict[str, Any]) -> str:
    role = str(item.get("role") or "").strip()
    if role:
        return role
    attrs = item.get("attributes") or {}
    if isinstance(attrs, dict):
        up_down = str(attrs.get("upDown") or "").strip()
        if up_down:
            return up_down
    return ""


def _outfit_id(row: dict[str, Any]) -> str:
    raw = row.get("outfit_id") or row.get("idMatch") or row.get("id")
    return str(raw).strip() if raw is not None else ""


def _outfit_source(row: dict[str, Any]) -> str:
    source = str(row.get("source") or "").strip()
    if source:
        return source
    shop = str(row.get("shopName") or "").strip()
    if "微导购" in shop:
        return "micro_guide"
    return "cc_material"


def _outfit_item_ids(row: dict[str, Any]) -> tuple[list[str], list[str]]:
    """从 items 抽取 sku_id / spu_id，去重且保序。"""
    items = row.get("items") or []
    sku_ids: list[str] = []
    spu_ids: list[str] = []
    if not isinstance(items, list):
        return sku_ids, spu_ids
    for it in items:
        if not isinstance(it, dict):
            continue
        sid = _item_sku_id(it)
        pid = _item_spu_id(it)
        if sid:
            sku_ids.append(sid)
        if pid:
            spu_ids.append(pid)
    return list(dict.fromkeys(sku_ids)), list(dict.fromkeys(spu_ids))


def _outfit_items(row: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in row.get("items") or []:
        if not isinstance(item, dict):
            continue
        sid = _item_sku_id(item)
        if not sid:
            continue
        price = item.get("price")
        out.append(
            {
                "sku_id": sid,
                "spu_id": _item_spu_id(item),
                "attrAlias": str(item.get("attrAlias") or sid),
                "idAlias": str(item.get("idAlias") or _item_spu_id(item)),
                "idGoods": item.get("idGoods"),
                "role": _item_role(item),
                "title": str(item.get("title") or ""),
                "category_l2": str(item.get("category_l2") or ""),
                "series": str(item.get("series") or ""),
                "price": float(price or 0.0),
                "display_image": _item_image(item),
                "tryon_image": str(item.get("tryon_image") or _item_image(item)),
                "is_master": bool(item.get("is_master") or item.get("isMaster")),
                "isMaster": bool(item.get("is_master") or item.get("isMaster")),
                "images": item.get("images") or {},
                "color": item.get("color") or {},
                "attributes": item.get("attributes") or {},
            },
        )
    return out


def outfit_doc(row: dict[str, Any]) -> dict[str, Any]:
    season = row.get("season") or []
    if isinstance(season, str):
        season = [season]
    attrs: dict[str, Any] = {}
    for item in row.get("items") or []:
        if isinstance(item, dict) and item.get("isMaster"):
            attrs = item.get("attributes") or {}
            if not isinstance(attrs, dict):
                attrs = {}
            break
    sku_ids, spu_ids = _outfit_item_ids(row)
    items = _outfit_items(row)
    roles = [x for x in (str(i.get("role") or "") for i in items) if x]
    display_image = str(
        row.get("display_image")
        or row.get("leftHeroUrl")
        or row.get("backgroundImg")
        or "",
    )
    master_item = next((i for i in items if i.get("is_master")), None)
    color_tags = outfit_color_series_tags({"items": items})
    return {
        "outfit_id": _outfit_id(row),
        "idMatch": row.get("idMatch") or _outfit_id(row),
        "name": str(row.get("name") or ""),
        "shopName": str(row.get("shopName") or ""),
        "type": row.get("type"),
        "flags": row.get("flags") or {},
        "leftHeroUrl": row.get("leftHeroUrl"),
        "backgroundImg": row.get("backgroundImg"),
        "search_text": str(row.get("search_text") or ""),
        "gender": (
            [str(x) for x in _ov]
            if isinstance((_ov := row.get("gender") or attrs.get("sex")), list)
            else ([str(_ov)] if _ov else [])
        ),
        "source": _outfit_source(row),
        "season": [str(x) for x in season],
        "series_tags": [str(x) for x in (row.get("series_tags") or [])],
        "color_series_tags": color_tags,
        "occasion_tags": [str(x) for x in (row.get("occasion_tags") or [])],
        "style_tags": [str(x) for x in (row.get("style_tags") or [])],
        "roles": list(dict.fromkeys([str(x) for x in (row.get("roles") or roles)])),
        "price_total": float(row.get("price_total") or 0.0),
        "status": int(row.get("status") or 0),
        "display_image": display_image,
        "index_images": _normalize_index_images(row.get("index_images")) or (
            [display_image] if display_image else []
        ),
        "background_img": str(row.get("background_img") or row.get("backgroundImg") or ""),
        "outfit_tryon_image": str(row.get("outfit_tryon_image") or row.get("tryon_result_image") or ""),
        "master_sku_id": str(row.get("master_sku_id") or (master_item or {}).get("sku_id") or ""),
        "master_spu_id": str(row.get("master_spu_id") or (master_item or {}).get("spu_id") or ""),
        "sku_ids": sku_ids,
        "spu_ids": spu_ids,
        "items": items,
    }


def bulk_index(
    client: Any,
    index: str,
    actions: Iterator[dict[str, Any]],
    batch_size: int,
) -> tuple[int, int]:
    ok = err = 0
    batch: list[dict[str, Any]] = []
    for action in actions:
        batch.append(action)
        if len(batch) >= batch_size:
            n, e = _flush_bulk(client, batch)
            ok += n
            err += e
            batch.clear()
    if batch:
        n, e = _flush_bulk(client, batch)
        ok += n
        err += e
    client.indices.refresh(index=index)
    return ok, err


def _flush_bulk(client: Any, batch: list[dict[str, Any]]) -> tuple[int, int]:
    if bulk is None:
        return 0, len(batch)
    try:
        res = bulk(client, batch, raise_on_error=False)
        if isinstance(res, tuple) and len(res) >= 2:
            success = int(res[0])
            errors = res[1]
            err_n = len(errors) if errors else 0
            return success, err_n
        return len(batch), 0
    except Exception as exc:
        logger.exception("bulk 失败: %s", exc)
        return 0, len(batch)


def prune_es_by_ids(client: Any, index: str, ids: list[str]) -> int:
    """按 _id 批量删除 ES 文档。"""
    if not ids or bulk is None:
        return 0
    deleted = 0
    chunk = 400
    for i in range(0, len(ids), chunk):
        part = ids[i : i + chunk]
        actions = [
            {"_op_type": "delete", "_index": index, "_id": oid}
            for oid in part
        ]
        res = bulk(client, actions, raise_on_error=False)
        if isinstance(res, tuple):
            deleted += int(res[0])
    client.indices.refresh(index=index)
    logger.info("ES 已删除孤立文档 index=%s count=%d", index, deleted)
    return deleted


def verify_skus_search(client: Any, index: str, sample_query: str) -> None:
    try:
        res = client.search(
            index=index,
            body={
                "size": 3,
                "query": {"match": {"search_text": sample_query}},
            },
        )
        hits = res.get("hits", {}).get("hits", [])
        logger.info("校验检索 match search_text=%r → %d 条", sample_query, len(hits))
        for h in hits[:3]:
            src = h.get("_source") or {}
            logger.info("  sku_id=%s", src.get("sku_id"))
    except Exception as exc:
        logger.warning("校验检索失败: %s", exc)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build FILA Elasticsearch indices from data/processed/*.jsonl",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="删除已有索引后重建（危险：清空数据）",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="仅写入相对状态文件内容发生变化文档（见 data/logs）",
    )
    parser.add_argument(
        "--prune-orphans",
        action="store_true",
        help="删除索引中已不在当前 JSONL 内的 _id（依赖状态文件记录的上次 id 集合）",
    )
    parser.add_argument(
        "--state-file",
        type=str,
        default="",
        help=f"状态 JSON 路径（默认 {DEFAULT_STATE_PATH}）",
    )
    parser.add_argument("--batch-size", type=int, default=400)
    parser.add_argument(
        "--skip-skus",
        action="store_true",
    )
    parser.add_argument(
        "--skip-outfits",
        action="store_true",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="跳过写入后的简单 match 校验",
    )
    args = parser.parse_args()

    state_path = (
        Path(args.state_file).expanduser().resolve()
        if args.state_file.strip()
        else DEFAULT_STATE_PATH
    )
    state = load_state(state_path)

    cfg = load_config()
    indices = get_elasticsearch_indices(cfg)
    ix_skus = indices["skus"]
    ix_outfits = indices["outfits"]

    client = connect_es(cfg)

    if not args.skip_skus:
        old_keys_sku = set(state["es"]["skus"])
        if args.reset:
            clear_es_bucket(state, "skus")
        delete_if_reset(client, ix_skus, args.reset)
        create_skus_index(client, ix_skus)
        path = PROCESSED / "skus.jsonl"
        if not path.is_file():
            logger.warning("缺少文件: %s", path)
        else:
            sku_rows: dict[str, dict[str, Any]] = {}
            file_hashes: dict[str, str] = {}
            filtered_empty_tryon = 0
            cs_total = 0
            cs_hit = 0
            for row in iter_jsonl(path):
                sid = str(row.get("sku_id") or "").strip()
                if not sid:
                    continue
                if sku_has_empty_tryon_image(row):
                    filtered_empty_tryon += 1
                    continue
                sku_rows[sid] = row
                file_hashes[sid] = doc_hash(sku_doc(row))
                cs_total += 1
                cs_val = map_color_to_series_list(
                    str(row.get("attr_name") or row.get("color_name") or ""),
                )
                if cs_val:
                    cs_hit += 1
            if filtered_empty_tryon:
                logger.info(
                    "SKU 跳过占位 tryon_image: %d 条",
                    filtered_empty_tryon,
                )
            if cs_total:
                logger.info(
                    "色系标签覆盖率: %d/%d = %.1f%%",
                    cs_hit,
                    cs_total,
                    cs_hit / cs_total * 100,
                )

            def gen_sku_actions() -> Iterator[dict[str, Any]]:
                for sid, fh in file_hashes.items():
                    if args.incremental:
                        if state["es"]["skus"].get(sid) == fh:
                            continue
                    row = sku_rows.get(sid)
                    if row is None:
                        continue
                    yield {
                        "_op_type": "index",
                        "_index": ix_skus,
                        "_id": sid,
                        "_source": sku_doc(row),
                    }

            n_ok, n_err = bulk_index(
                client,
                ix_skus,
                gen_sku_actions(),
                args.batch_size,
            )
            state["es"]["skus"] = file_hashes
            if args.prune_orphans:
                dead = list(old_keys_sku - set(file_hashes.keys()))
                prune_es_by_ids(client, ix_skus, dead)
            logger.info(
                "SKU 索引 %s: 成功=%d 失败=%d（incremental=%s）",
                ix_skus,
                n_ok,
                n_err,
                args.incremental,
            )
            if not args.no_verify:
                verify_skus_search(client, ix_skus, "FILA")

    if not args.skip_outfits:
        old_keys_out = set(state["es"]["outfits"])
        if args.reset:
            clear_es_bucket(state, "outfits")
        delete_if_reset(client, ix_outfits, args.reset)
        create_outfits_index(client, ix_outfits)
        path = PREVIEW_OUTFITS
        if not path.is_file():
            logger.warning("缺少文件: %s", path)
        else:
            file_hashes = {}
            outfit_rows: dict[str, dict[str, Any]] = {}
            with path.open(encoding="utf-8") as f:
                rows = json.load(f)
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                oid = _outfit_id(row)
                if not oid:
                    continue
                doc = outfit_doc(row)
                outfit_rows[oid] = row
                file_hashes[oid] = doc_hash(doc)

            def gen_outfit_actions() -> Iterator[dict[str, Any]]:
                for oid, fh in file_hashes.items():
                    if args.incremental:
                        if state["es"]["outfits"].get(oid) == fh:
                            continue
                    row = outfit_rows.get(oid)
                    if row is None:
                        continue
                    yield {
                        "_op_type": "index",
                        "_index": ix_outfits,
                        "_id": oid,
                        "_source": outfit_doc(row),
                    }

            n_ok, n_err = bulk_index(
                client,
                ix_outfits,
                gen_outfit_actions(),
                args.batch_size,
            )
            state["es"]["outfits"] = file_hashes
            if args.prune_orphans:
                dead = list(old_keys_out - set(file_hashes.keys()))
                prune_es_by_ids(client, ix_outfits, dead)
            logger.info(
                "搭配索引 %s: 成功=%d 失败=%d",
                ix_outfits,
                n_ok,
                n_err,
            )

    save_state(state, state_path)
    print(
        "\n完成。状态已写入:",
        state_path,
        "\n请在 config.yaml 中设置 elasticsearch.enabled: true 以启用检索。\n"
        f"  indices: skus={ix_skus}, outfits={ix_outfits}\n",
    )


if __name__ == "__main__":
    main()
