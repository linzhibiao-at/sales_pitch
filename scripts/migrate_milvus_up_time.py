#!/usr/bin/env python3
"""原地迁移 Milvus collection 的 up_time 字段（不 drop、不停服、不重新 embed）。

背景：fila_sku_vectors / fila_sku_complementary 集合已有 up_time 字段，但大量行值为 0
（建集合时 skus.jsonl 未填 / 旧版 build 未写 up_time）。导致 recommend.up_time_since
过滤（up_time >= 阈值）把这些行错杀 → 召回空。

做法：query 回每行的 向量 + 全部标量，按 sku_id 从 skus.jsonl 取 up_time（up_time_to_epoch），
upsert 回去（PK 相同即覆盖，向量原样保留）。无需 drop / 重建 / 重新 embed。

幂等：重跑只会把 up_time 设成 skus.jsonl 的当前值。--dry-run 只统计不写。

用法（fila_agent_html 目录）::

  export PYTHONPATH="$(pwd)"
  python scripts/migrate_milvus_up_time.py                     # 两个集合都跑
  python scripts/migrate_milvus_up_time.py --collection fila_sku_vectors
  python scripts/migrate_milvus_up_time.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("migrate_up_time")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.etl_common import up_time_to_epoch  # noqa: E402

# 每个 collection：pk / vec / sku_id 字段 / 需 query+upsert 的标量（含 up_time）
# 字段清单与 migrate_milvus_add_attr_fields.COLLECTIONS 对齐。
COLLECTIONS: list[dict[str, Any]] = [
    {
        "name": "fila_sku_vectors",
        "pk": "doc_id",
        "vec": "product_vector",
        "sku_id_field": "sku_id",
        "scalars": [
            "spu_id", "product_name", "product_image", "color_series", "category_l2",
            "role", "gender", "season", "sku_id_for_group", "layer", "coverage",
            "length_class", "is_intimate", "scene_domain", "series", "group_brand",
            "modeling", "price", "age", "up_time", "color_name", "id_goods",
        ],
    },
    {
        "name": "fila_sku_complementary_vectors",
        "pk": "sku_id",
        "vec": "complementary_vector",
        "sku_id_field": "sku_id",
        "scalars": [
            "spu_id", "role", "category_l2", "gender", "season", "color_series",
            "layer", "coverage", "length_class", "is_intimate", "scene_domain",
            "series", "group_brand", "modeling", "price", "age", "up_time",
            "color_name", "id_goods",
        ],
    },
    # hybrid 集合：sparse_vector 由服务端 BM25 Function 从 search_text 自动生成，
    # 客户端不填也不查询（无法客户端重建）。upsert 时务必带上 search_text 与 dense_vector，
    # 让 Function 重新生成 sparse_vector——否则会破坏 hybrid 检索。
    # 历史问题：build_hybrid_index build_insert_row 曾用 int(float(date_str)) 写 up_time
    # 全部落 0（已修）；此条用 upsert 原地补 up_time，无需重建/重新 embed。
    {
        "name": "fila_sku_hybrid_vectors",
        "pk": "sku_id",
        "vec": "dense_vector",
        "sku_id_field": "sku_id",
        "scalars": [
            "search_text",
            "title", "product_name_short", "goods_sn", "brand_line",
            "category", "category_l1", "category_l2", "up_down_raw", "role",
            "color_name", "color_series", "gender", "season", "series",
            "sub_series", "year", "modeling", "length", "length_class",
            "layer", "coverage", "is_intimate", "scene_domain", "group_brand",
            "technology", "features", "selling_point_label", "material", "age",
            "price", "market_price", "min_price", "max_price", "onsell",
            "sales", "sales_week", "sales_month", "w_order", "id_goods", "sku_count",
        ],
    },
]


def load_up_time_map() -> dict[str, int]:
    """sku_id -> up_time epoch（来自 data/processed/skus.jsonl）。"""
    from backend.config import load_config

    cfg = load_config()
    proc = ROOT / (cfg.get("paths") or {}).get("processed_dir", "data/processed")
    path = proc / "skus.jsonl"
    out: dict[str, int] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            sid = str(r.get("sku_id") or "").strip()
            if sid:
                out[sid] = up_time_to_epoch(r.get("up_time"))
    logger.info("loaded %d sku up_time from %s", len(out), path)
    return out


def migrate_one(client: Any, coll: dict[str, Any], up_map: dict[str, int], *,
                 dry_run: bool, batch_size: int = 500) -> None:
    name = coll["name"]
    pk = coll["pk"]
    vec = coll["vec"]
    sid_field = coll["sku_id_field"]
    output_fields = [pk, vec, sid_field, "up_time"] + [
        s for s in coll["scalars"] if s not in (pk, vec, sid_field, "up_time")
    ]
    # 去重保序
    seen = set()
    output_fields = [x for x in output_fields if not (x in seen or seen.add(x))]

    if not client.has_collection(name):
        logger.warning("collection %s 不存在，跳过", name)
        return

    updated = unchanged = no_sku = total = 0
    batch: list[dict[str, Any]] = []
    # query_iterator 全量扫描（自动分页，突破 16384 上限）
    iterator = client.query_iterator(
        collection_name=name,
        filter=f'{pk} != ""',
        output_fields=output_fields,
        batch_size=2000,
        limit=99999,
    )
    while True:
        batch_rows = iterator.next()
        if not batch_rows:
            break
        for r in batch_rows:
            total += 1
            sid = str(r.get(sid_field) or "").strip()
            new_ut = up_map.get(sid)
            if new_ut is None:
                no_sku += 1
                continue  # 该行 sku 不在 skus.jsonl（非 FILA / 已删），保留原值不动
            if int(r.get("up_time") or 0) == new_ut:
                unchanged += 1
                continue
            r["up_time"] = int(new_ut)
            batch.append(r)
            if len(batch) >= batch_size and not dry_run:
                client.upsert(name, batch)
                updated += len(batch)
                batch.clear()
                logger.info("[%s] upsert 进度 %d (扫描 %d)", name, updated, total)
    if dry_run:
        updated = len(batch)
    elif batch:
        client.upsert(name, batch)
        updated += len(batch)
    logger.info(
        "[%s] done: total=%d updated=%d unchanged=%d no_sku=%d dry_run=%s",
        name, total, updated, unchanged, no_sku, dry_run,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="原地迁移 Milvus up_time 字段")
    parser.add_argument("--collection", type=str, default="", help="只跑指定集合")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写")
    args = parser.parse_args()

    from backend.config import (
        get_milvus_token,
        get_milvus_uri,
        load_config,
        restore_stashed_milvus_uri,
        stash_milvus_db_uri_before_pymilvus_import,
    )
    from pymilvus import MilvusClient

    cfg = load_config()
    mv = cfg.get("milvus") or {}
    uri_env = str(mv.get("uri_env") or "FILA_MILVUS_URI")
    stash_milvus_db_uri_before_pymilvus_import(uri_env)
    restore_stashed_milvus_uri()
    client = MilvusClient(uri=get_milvus_uri(cfg), token=get_milvus_token(cfg) or None)

    up_map = load_up_time_map()
    targets = [c for c in COLLECTIONS if not args.collection or c["name"] == args.collection]
    for coll in targets:
        migrate_one(client, coll, up_map, dry_run=args.dry_run)
    logger.info("all done")


if __name__ == "__main__":
    main()
