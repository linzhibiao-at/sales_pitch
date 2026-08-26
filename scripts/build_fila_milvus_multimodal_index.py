#!/usr/bin/env python3
"""FILA fila_agent：基于 processed JSONL 构建 Milvus 多图向量索引。

每个 SKU 的 index_images 数组中每张图片生成一个 doc，主键 doc_id 格式：
  {sku_id}_{SHA1(image_url)[:12]}

运行时使用 group_by_field="sku_id" 分组检索，同一 SKU 仅返回相似度最高的那张图。

数据源（相对 fila_agent 目录）：
  - data/processed/skus.jsonl：一条 SKU 一条行（字段 index_images 数组）

支持两种 Milvus 模式（由 config.yaml ``milvus.mode`` 控制）：
  - **local**（默认）：Milvus Lite 本地文件 ``data/milvus_local/fila_milvus.db``
  - **cloud**：阿里云托管 Milvus（``milvus.cloud.uri`` + 环境变量密码）

集合名与 config.yaml 中 milvus.collections 一致：
  - fila_sku_vectors（主键 doc_id，group_by sku_id）

用法（在 fila_agent 目录下）::

  source .venv/bin/activate
  export PYTHONPATH="$(pwd)"
  export ARK_API_KEY=...

  # 本地 Milvus Lite（默认）
  python3 scripts/build_fila_milvus_multimodal_index.py [--reset] [--incremental] [--batch-size 128]

  # 云端 Milvus
  export FILA_MILVUS_MODE=cloud
  export FILA_MILVUS_PASSWORD='your-password'
  python3 scripts/build_fila_milvus_multimodal_index.py [--reset] [--incremental] [--batch-size 128]

更新策略：
  - **全量**：默认对所有行执行 ``upsert``（同主键覆盖）。``--reset`` 删除集合并重建 schema。
  - **增量**：``--incremental`` 仅当 ``index_images`` 数组 + 向量维度 + embedding 模型名
    相对状态文件变化时重算向量并 upsert（省 API）。
  - **孤立向量**：``--prune-orphans`` 删除「状态中有、当前 JSONL 已无」的 doc_id。

状态文件：``data/logs/fila_index_sync_state.json``（可用 ``--state-file`` 覆盖）。

依赖：见 requirements.txt（pymilvus、milvus、setuptools 版本约束）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Iterator, List

import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
PROCESSED_DIR = ROOT / "data" / "processed"
DEFAULT_DB = ROOT / "data" / "milvus_local" / "fila_milvus.db"
CONFIG_PATH = ROOT / "config.yaml"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from index_sync_state import (
    DEFAULT_STATE_PATH,
    clear_milvus_bucket,
    load_state,
    milvus_row_signature,
    save_state,
)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from backend.empty_image_urls import sku_has_empty_tryon_image
from backend.intent.color_series_mapper import map_color_to_series_list
from backend.intent.sku_attributes import enrich_sku_attributes
from scripts.etl_common import up_time_to_epoch


def load_yaml_config() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def ensure_backend_path() -> None:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def get_embedding_client():
    """延迟导入，便于在未配置 API Key 时先做 --help。"""
    ensure_backend_path()
    from backend.embedding_client import embed_image_url

    return embed_image_url


def _sha1_short(url: str, length: int = 12) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:length]


def _doc_id(sku_id: str, image_url: str) -> str:
    return f"{sku_id}_{_sha1_short(image_url)}"


try:
    from pymilvus import DataType, MilvusClient
except ImportError as exc:
    MilvusClient = None  # type: ignore
    DataType = None  # type: ignore
    _IMPORT_ERR = exc
else:
    _IMPORT_ERR = None


def create_sku_collection(client: Any, name: str, dim: int) -> None:
    schema = client.create_schema()
    schema.add_field(
        "doc_id",
        DataType.VARCHAR,
        max_length=128,
        is_primary=True,
        auto_id=False,
    )
    schema.add_field("sku_id", DataType.VARCHAR, max_length=64)
    schema.add_field("product_vector", DataType.FLOAT_VECTOR, dim=dim)
    schema.add_field("spu_id", DataType.VARCHAR, max_length=32)
    schema.add_field("product_name", DataType.VARCHAR, max_length=512)
    schema.add_field("product_image", DataType.VARCHAR, max_length=1024)
    schema.add_field("color_series", DataType.ARRAY, element_type=DataType.VARCHAR, max_length=32, max_capacity=8)
    schema.add_field("color_name", DataType.VARCHAR, max_length=64)
    schema.add_field("category_l2", DataType.VARCHAR, max_length=64)
    schema.add_field("role", DataType.VARCHAR, max_length=32)
    schema.add_field(
        "gender",
        DataType.ARRAY,
        element_type=DataType.VARCHAR,
        max_length=32,
        max_capacity=8,
    )
    schema.add_field("season", DataType.VARCHAR, max_length=256)
    # 结构化属性（召回阶段 expr 过滤用，值来自 skus.jsonl 的 extract_* 推导）
    schema.add_field("layer", DataType.VARCHAR, max_length=16)
    schema.add_field("coverage", DataType.VARCHAR, max_length=16)
    schema.add_field("length_class", DataType.VARCHAR, max_length=16)
    schema.add_field("is_intimate", DataType.VARCHAR, max_length=8)
    schema.add_field("scene_domain", DataType.VARCHAR, max_length=32)
    schema.add_field("series", DataType.VARCHAR, max_length=64)
    schema.add_field("group_brand", DataType.VARCHAR, max_length=64)
    schema.add_field("modeling", DataType.VARCHAR, max_length=16)
    schema.add_field("price", DataType.DOUBLE)
    schema.add_field("age", DataType.VARCHAR, max_length=16)
    schema.add_field("up_time", DataType.INT64)
    schema.add_field("id_goods", DataType.INT64)
    # 为 group_by_field="sku_id" 建标量索引（提升分组检索性能）
    schema.add_field(
        "sku_id_for_group",
        DataType.VARCHAR,
        max_length=64,
    )
    client.create_collection(name, schema=schema)
    idx = client.prepare_index_params()
    idx.add_index(
        "product_vector",
        index_type="AUTOINDEX",
        index_name="sku_vec_idx",
        metric_type="COSINE",
    )
    # up_time 标量倒排索引：支持按上市时间范围过滤与推新排序
    idx.add_index(
        "up_time",
        index_type="INVERTED",
        index_name="up_time_inv_idx",
    )
    # group_brand 倒排索引：低基数枚举，按集团品牌过滤召回
    idx.add_index(
        "group_brand",
        index_type="INVERTED",
        index_name="group_brand_inv_idx",
    )
    client.create_index(name, idx)
    client.load_collection(name)
    logger.info("Created collection: %s", name)


def _chunks(ids: List[str], size: int) -> Iterator[List[str]]:
    for i in range(0, len(ids), size):
        yield ids[i : i + size]


def verify_search(
    client: Any,
    collection: str,
    sample_vector: list[float],
    id_field: str,
) -> bool:
    logger.info("Verify search on %s ...", collection)
    try:
        res = client.search(
            collection_name=collection,
            data=[sample_vector],
            anns_field="product_vector",
            limit=3,
            output_fields=[id_field, "sku_id"],
            group_by_field="sku_id",
        )
        hits = res[0] if res else []
        if not hits:
            logger.error("Verify failed: empty hits")
            return False
        for i, h in enumerate(hits, 1):
            ent = h.get("entity", h)
            dist = getattr(h, "distance", None)
            if dist is None and isinstance(h, dict):
                dist = h.get("distance")
            logger.info(
                "  #%d %s=%s sku_id=%s dist=%s",
                i,
                id_field,
                ent.get(id_field),
                ent.get("sku_id"),
                dist,
            )
        return True
    except Exception as exc:
        logger.exception("Verify failed: %s", exc)
        return False


def index_skus(
    client: Any,
    collection: str,
    embed_fn,
    dim: int,
    batch_size: int,
    test_limit: int,
    *,
    embedding_model: str,
    incremental: bool,
    prune_orphans: bool,
    state: dict[str, Any],
    skip_state: bool,
    prior_doc_ids: set[str],
) -> tuple[int, int, list[float] | None]:
    path = PROCESSED_DIR / "skus.jsonl"
    if not path.is_file():
        logger.warning("Missing %s", path)
        return 0, 0, None

    # ── 读取 JSONL，收集每个 SKU 的 index_images ──
    sku_rows: dict[str, dict[str, Any]] = {}
    file_sigs: dict[str, str] = {}
    # 记录每个 SKU 对应的所有 doc_id（用于增量判断和孤立清理）
    sku_to_doc_ids: dict[str, list[str]] = {}
    filtered_empty_tryon = 0

    for row in iter_jsonl(path):
        sku_id = str(row.get("sku_id") or "").strip()
        imgs_raw = row.get("index_images") or []
        if isinstance(imgs_raw, str):
            try:
                imgs_raw = json.loads(imgs_raw)
            except (ValueError, TypeError):
                imgs_raw = []
        imgs = [str(u).strip() for u in imgs_raw if str(u).strip() and str(u).strip().lower() != "nan"]
        if not sku_id or not imgs:
            continue
        if sku_has_empty_tryon_image(row):
            filtered_empty_tryon += 1
            continue
        sku_rows[sku_id] = row
        # 签名基于整个 index_images 数组
        file_sigs[sku_id] = milvus_row_signature(
            imgs,
            dimensions=dim,
            embedding_model=embedding_model,
        )
        # 计算该 SKU 对应的所有 doc_id
        sku_to_doc_ids[sku_id] = [_doc_id(sku_id, u) for u in imgs]

    if filtered_empty_tryon:
        logger.info(
            "SKU 跳过占位 tryon_image: %d 条",
            filtered_empty_tryon,
        )

    # 确定需要重建的 SKU（签名变化或全量）
    target_sku_ids = [
        sid
        for sid, sig in file_sigs.items()
        if (not incremental)
        or state["milvus"]["sku_vectors"].get(sid) != sig
    ]
    if test_limit > 0:
        target_sku_ids = target_sku_ids[:test_limit]
        logger.info("TEST mode: at most %d SKU upserts", len(target_sku_ids))

    # ── 为每个 target SKU 的每张图片生成 embedding 并 upsert ──
    batch: list[dict[str, Any]] = []
    ok = skip = 0
    bad_emb_detail = 0
    bad_emb_cap = 5
    first_vec: list[float] | None = None
    all_new_doc_ids: set[str] = set()
    total = len(target_sku_ids)

    for idx, sku_id in enumerate(target_sku_ids, 1):
        row = sku_rows.get(sku_id)
        if row is None:
            skip += 1
            continue
        # 兜底推导结构化属性（jsonl 理论上已有，缺失则按 title+category_l2 实时推导）
        enrich_sku_attributes(row)
        imgs_raw = row.get("index_images") or []
        if isinstance(imgs_raw, str):
            try:
                imgs_raw = json.loads(imgs_raw)
            except (ValueError, TypeError):
                imgs_raw = []
        imgs = [str(u).strip() for u in imgs_raw if str(u).strip()]
        title = str(row.get("title") or "")[:500]
        season = row.get("season") or []
        if isinstance(season, list):
            season_s = ",".join(str(x) for x in season if x)[:256]
        else:
            season_s = str(season)[:256]
        color_series = [s[:32] for s in map_color_to_series_list(
            str(row.get("attr_name") or row.get("color_name") or ""),
        )][:8]

        sku_ok_count = 0
        for img_url in imgs:
            did = _doc_id(sku_id, img_url)
            all_new_doc_ids.add(did)
            vec = embed_fn(img_url)
            if not vec or len(vec) != dim:
                skip += 1
                if bad_emb_detail < bad_emb_cap:
                    logger.warning(
                        "[%d/%d] bad embedding sku_id=%s url=%s len=%s",
                        idx,
                        total or 1,
                        sku_id,
                        img_url[:80],
                        len(vec) if vec else None,
                    )
                    bad_emb_detail += 1
                continue
            if first_vec is None:
                first_vec = vec
            sku_ok_count += 1
            batch.append(
                {
                    "doc_id": did[:128],
                    "sku_id": sku_id[:64],
                    "product_vector": vec,
                    "spu_id": str(row.get("spu_id") or "")[:32],
                    "product_name": title[:512],
                    "product_image": img_url[:1024],
                    "color_series": color_series,
                    "color_name": str(row.get("color_name") or row.get("attr_name") or "")[:64],
                    "category_l2": str(row.get("category_l2") or "")[:64],
                    "role": str(row.get("role") or "")[:32],
                    "gender": [str(x)[:32] for x in row["gender"]] if isinstance(row.get("gender"), list) else ([str(row.get("gender"))[:32]] if row.get("gender") else []),
                    "season": season_s,
                    "layer": str(row.get("layer") or "")[:16],
                    "coverage": str(row.get("coverage") or "")[:16],
                    "length_class": str(row.get("length_class") or "")[:16],
                    "is_intimate": "true" if row.get("is_intimate") else "false",
                    "scene_domain": str(row.get("scene_domain") or "")[:32],
                    "series": str(row.get("series") or "")[:64],
                    "group_brand": str(row.get("group_brand") or "")[:64],
                    "modeling": str(row.get("modeling") or "")[:16],
                    "price": float(row.get("price") or 0.0),
                    "age": str(row.get("age") or "")[:16],
                    "up_time": up_time_to_epoch(row.get("up_time")),
                    "id_goods": int(row.get("id_goods") or row.get("goods_id") or 0),
                    "sku_id_for_group": sku_id[:64],
                },
            )
            if len(batch) >= batch_size:
                client.upsert(collection, batch)
                ok += len(batch)
                batch.clear()
                logger.info(
                    "[%d/%d] doc upserted=%d skip=%d",
                    idx, total, ok, skip,
                )
                time.sleep(0.02)
        if sku_ok_count == 0 and imgs:
            logger.warning(
                "[%d/%d] sku_id=%s 所有图片 embedding 均失败",
                idx, total, sku_id,
            )

    if batch:
        client.upsert(collection, batch)
        ok += len(batch)
    client.flush(collection)

    # ── 状态管理 ──
    if not skip_state:
        state["milvus"]["sku_vectors"] = file_sigs
        # 记录当前所有 doc_id（用于下次孤立清理）
        state["milvus"]["doc_ids"] = list(all_new_doc_ids)
        if prune_orphans and prior_doc_ids:
            dead = list(prior_doc_ids - all_new_doc_ids)
            for part in _chunks(dead, 200):
                client.delete(collection, ids=part)
            logger.info("Milvus 删除孤立 doc_id: %d", len(dead))

    if skip and ok == 0 and total:
        logger.error(
            "SKU 向量全部失败：请查看上方 embedding 告警。"
            "常见原因：未导出密钥、网关返回 4xx、"
            "返回向量维度与 embedding.dimensions 不一致。",
        )
    logger.info(
        "SKU done: sku=%d, doc_upserted=%d, doc_skipped=%d",
        total, ok, skip,
    )
    return ok, skip, first_vec


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build FILA Milvus multimodal index (one doc per index_image)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop existing collection before rebuild",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="仅对签名变化的 SKU 重算向量并 upsert",
    )
    parser.add_argument(
        "--prune-orphans",
        action="store_true",
        help="删除集合中已不在 JSONL 的 doc_id",
    )
    parser.add_argument(
        "--state-file",
        type=str,
        default="",
        help=f"状态 JSON 路径（默认 {DEFAULT_STATE_PATH}）",
    )
    parser.add_argument("--batch-size", type=int, default=1024)
    parser.add_argument(
        "--test",
        type=int,
        default=0,
        metavar="N",
        help="Only process first N rows (smoke test)",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default="",
        help="(已废弃，请使用 FILA_MILVUS_URI 或 config.yaml milvus 配置) "
             "Milvus Lite db file，仅 local 模式有效",
    )
    args = parser.parse_args()

    if _IMPORT_ERR is not None or MilvusClient is None:
        logger.error(
            "需要 pymilvus、milvus（Lite），见 requirements.txt",
        )
        raise SystemExit(1) from _IMPORT_ERR

    state_path = (
        Path(args.state_file).expanduser().resolve()
        if args.state_file.strip()
        else DEFAULT_STATE_PATH
    )
    state = load_state(state_path)

    # prior_doc_ids: 上次状态中记录的所有 doc_id
    prior_doc_ids = set(state["milvus"].get("doc_ids") or [])

    cfg = load_yaml_config()
    emb_cfg = cfg.get("embedding") or {}
    dim = int(emb_cfg.get("dimensions") or 1024)
    embedding_model = str(emb_cfg.get("model") or "")
    mv_cfg = cfg.get("milvus") or {}
    sku_coll = str(
        mv_cfg.get("collections", {}).get("sku_vectors") or "fila_sku_vectors",
    )

    # ── 解析 Milvus 连接（复用 backend.config 的云端/本地统一逻辑） ──
    from backend.config import (
        get_milvus_token,
        get_milvus_uri,
        restore_stashed_milvus_uri,
        stash_milvus_db_uri_before_pymilvus_import,
    )

    uri_env = str(mv_cfg.get("uri_env") or "FILA_MILVUS_URI")
    stash_milvus_db_uri_before_pymilvus_import(uri_env)
    # pymilvus 已在顶部 import，此处 restore
    restore_stashed_milvus_uri()

    # --db-path 仅作为 local 模式的 fallback
    if args.db_path:
        uri = str(Path(args.db_path).expanduser().resolve())
        token = ""
        logger.info("Using --db-path (local override): %s", uri)
    else:
        uri = get_milvus_uri(cfg)
        token = get_milvus_token(cfg)
    if not uri:
        raise SystemExit(
            "无法解析 Milvus URI：请设置 FILA_MILVUS_URI，"
            "或 config milvus.mode=cloud + cloud.uri，"
            "或 milvus.local_data_file",
        )

    embed_image_url = get_embedding_client()

    logger.info("Milvus URI: %s", uri)
    client = MilvusClient(uri=uri, token=token or None)

    if args.reset:
        clear_milvus_bucket(state, "sku_vectors")
        # 也清除 doc_ids
        state["milvus"]["doc_ids"] = []
        if client.has_collection(sku_coll):
            client.drop_collection(sku_coll)
            logger.info("Dropped collection: %s", sku_coll)

    if not client.has_collection(sku_coll):
        create_sku_collection(client, sku_coll, dim)

    test_n = args.test if args.test > 0 else 0
    skip_state = test_n > 0
    prune_doc_ids = (
        prior_doc_ids
        if (args.prune_orphans and not args.reset)
        else set()
    )

    _, _, sku_first = index_skus(
        client,
        sku_coll,
        embed_image_url,
        dim,
        args.batch_size,
        test_n,
        embedding_model=embedding_model,
        incremental=args.incremental,
        prune_orphans=args.prune_orphans,
        state=state,
        skip_state=skip_state,
        prior_doc_ids=prune_doc_ids,
    )

    if not skip_state:
        save_state(state, state_path)

    if test_n > 0:
        if sku_first is None:
            logger.error(
                "TEST: 无 doc 向量写入，请检查 ARK_API_KEY 与网络后重试",
            )
            raise SystemExit(1)
        if not verify_search(client, sku_coll, sku_first, "doc_id"):
            raise SystemExit(1)
        logger.info("Smoke test OK")

    print(f"\n索引构建结束。Milvus URI: {uri}")
    if not skip_state:
        print(f"状态文件: {state_path}")


if __name__ == "__main__":
    main()
