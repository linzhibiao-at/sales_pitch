#!/usr/bin/env python3
"""FILA Milvus hybrid 索引（search_text+BM25+sparse_vector / dense_vector）。

复刻 descent schema_manager + build_index，适配 fila 数据源（skus.jsonl）与配置。
sparse_vector 由服务端 BM25 Function 从 search_text 自动生成，客户端不填。

用法（在 fila_agent_html 目录）::

  source .venv/bin/activate
  export PYTHONPATH="$(pwd)"
  export ARK_API_KEY=...
  python3 scripts/build_hybrid_index.py [--reset] [--incremental] [--limit N] [--batch-size 500]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Iterator

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("build_hybrid_index")

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from index_sync_state import (  # noqa: E402
    DEFAULT_STATE_PATH,
    clear_milvus_bucket,
    load_state,
    milvus_text_row_signature,
    save_state,
)
from etl_common import up_time_to_epoch  # noqa: E402
from scripts.hybrid_text import build_keyword_text, build_semantic_text  # noqa: E402

DataType = CollectionSchema = FieldSchema = Function = FunctionType = MilvusClient = None  # type: ignore


def _import_pymilvus() -> None:
    global DataType, CollectionSchema, FieldSchema, Function, FunctionType, MilvusClient
    if MilvusClient is not None:
        return
    from pymilvus import (  # type: ignore
        CollectionSchema,
        DataType,
        FieldSchema,
        Function,
        FunctionType,
        MilvusClient as _MC,
    )
    globals()["DataType"] = DataType
    globals()["CollectionSchema"] = CollectionSchema
    globals()["FieldSchema"] = FieldSchema
    globals()["Function"] = Function
    globals()["FunctionType"] = FunctionType
    MilvusClient = _MC


def load_yaml_config() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# 标量字段：(name, kind, kwargs)。kind 决定 build_insert_row / schema 的取值方式。
# 字段名与 fila sku row 键一致（title / season / ...），无需源键映射。
_SCALAR_FIELDS: list[tuple[str, str, dict[str, Any]]] = [
    ("title", "VARCHAR", {"max_length": 256}),
    ("product_name_short", "VARCHAR", {"max_length": 128}),
    ("goods_sn", "VARCHAR", {"max_length": 64}),
    ("brand_line", "VARCHAR", {"max_length": 64}),
    ("category", "VARCHAR", {"max_length": 128}),
    ("category_l1", "VARCHAR", {"max_length": 32}),
    ("category_l2", "VARCHAR", {"max_length": 64}),
    ("up_down_raw", "VARCHAR", {"max_length": 32}),
    ("role", "VARCHAR", {"max_length": 32}),
    ("color_name", "VARCHAR", {"max_length": 64}),
    ("color_series", "ARRAY", {"element_type": "VARCHAR", "max_length": 32, "max_capacity": 8}),
    ("gender", "ARRAY", {"element_type": "VARCHAR", "max_length": 32, "max_capacity": 8}),
    ("season", "VARCHAR", {"max_length": 256}),
    ("series", "VARCHAR", {"max_length": 64}),
    ("sub_series", "VARCHAR", {"max_length": 128}),
    ("year", "VARCHAR", {"max_length": 16}),
    ("modeling", "VARCHAR", {"max_length": 16}),
    ("length", "VARCHAR", {"max_length": 16}),
    ("length_class", "VARCHAR", {"max_length": 16}),
    ("layer", "VARCHAR", {"max_length": 16}),
    ("coverage", "VARCHAR", {"max_length": 16}),
    ("is_intimate", "VARCHAR", {"max_length": 8}),
    ("scene_domain", "VARCHAR", {"max_length": 32}),
    ("group_brand", "VARCHAR", {"max_length": 64}),
    ("technology", "VARCHAR", {"max_length": 512}),
    ("features", "VARCHAR", {"max_length": 1024}),
    ("selling_point_label", "VARCHAR", {"max_length": 128}),
    ("material", "VARCHAR", {"max_length": 1024}),
    ("age", "VARCHAR", {"max_length": 16}),
    ("price", "DOUBLE", {}),
    ("market_price", "DOUBLE", {}),
    ("min_price", "DOUBLE", {}),
    ("max_price", "DOUBLE", {}),
    ("onsell", "INT64", {}),
    ("sales", "INT64", {}),
    ("sales_week", "INT64", {}),
    ("sales_month", "INT64", {}),
    ("w_order", "INT64", {}),
    ("up_time", "INT64", {}),
    ("id_goods", "INT64", {}),
    ("sku_count", "INT64", {}),
]


def build_hybrid_schema(dim: int) -> Any:
    """构造 hybrid 集合 schema（search_text chinese analyzer + sparse_vector + dense_vector + 标量 + BM25 Function）。"""
    _import_pymilvus()
    fields = [
        FieldSchema(name="sku_id", dtype=DataType.VARCHAR, max_length=64, is_primary=True, auto_id=False),
        FieldSchema(
            name="search_text",
            dtype=DataType.VARCHAR,
            max_length=8192,
            enable_analyzer=True,
            analyzer_params={"type": "chinese"},
        ),
        FieldSchema(name="sparse_vector", dtype=DataType.SPARSE_FLOAT_VECTOR),
        FieldSchema(name="dense_vector", dtype=DataType.FLOAT_VECTOR, dim=dim),
    ]
    for name, kind, kw in _SCALAR_FIELDS:
        if kind == "ARRAY":
            fields.append(
                FieldSchema(
                    name=name,
                    dtype=DataType.ARRAY,
                    element_type=DataType.VARCHAR,
                    max_length=kw["max_length"],
                    max_capacity=kw["max_capacity"],
                )
            )
        elif kind == "VARCHAR":
            fields.append(FieldSchema(name=name, dtype=DataType.VARCHAR, max_length=kw["max_length"]))
        elif kind == "DOUBLE":
            fields.append(FieldSchema(name=name, dtype=DataType.DOUBLE))
        elif kind == "INT64":
            fields.append(FieldSchema(name=name, dtype=DataType.INT64))
    bm25_function = Function(
        name="search_text_bm25",
        input_field_names=["search_text"],
        output_field_names=["sparse_vector"],
        function_type=FunctionType.BM25,
    )
    return CollectionSchema(
        fields=fields,
        functions=[bm25_function],
        description="FILA SKU hybrid (BM25+dense) search collection",
        enable_dynamic_field=False,
    )


def get_hybrid_index_params() -> list[dict[str, Any]]:
    return [
        {
            "field_name": "sparse_vector",
            "index_name": "idx_sparse",
            "index_type": "SPARSE_INVERTED_INDEX",
            "metric_type": "BM25",
            "params": {"drop_ratio_build": 0.2},
        },
        {
            "field_name": "dense_vector",
            "index_name": "idx_dense",
            "index_type": "IVF_FLAT",
            "metric_type": "COSINE",
            "params": {"nlist": 64},
        },
        {"field_name": "up_time", "index_name": "up_time_inv_idx", "index_type": "INVERTED"},
        {"field_name": "group_brand", "index_name": "group_brand_inv_idx", "index_type": "INVERTED"},
    ]


def create_hybrid_collection(client: Any, name: str, dim: int, uri: str) -> None:
    """建集合 + 索引（随 create_collection 一次落）。local *.db 报错。"""
    _import_pymilvus()
    from backend.config import is_milvus_lite_local_uri

    if is_milvus_lite_local_uri(uri):
        raise SystemExit(
            "BM25 Function 在 Milvus Lite(*.db)下不支持，请用 cloud Milvus(uri=http://...):"
            " 设 FILA_MILVUS_MODE=cloud 或 FILA_MILVUS_URI"
        )
    if client.has_collection(name):
        logger.info("Collection already exists: %s", name)
        return
    schema = build_hybrid_schema(dim)
    index_params = client.prepare_index_params()
    for cfg in get_hybrid_index_params():
        index_params.add_index(**cfg)
    client.create_collection(collection_name=name, schema=schema, index_params=index_params)
    logger.info("Created hybrid collection: %s (dim=%d)", name, dim)


def build_insert_row(row: dict[str, Any], vec: list[float], dim: int) -> dict[str, Any]:
    """单行 Milvus insert 记录：search_text 原文 + dense_vector + 标量；不含 sparse_vector。"""
    search_text = build_keyword_text(row)[:8192]
    rec: dict[str, Any] = {
        "sku_id": str(row.get("sku_id") or "")[:64],
        "search_text": search_text,
        "dense_vector": vec,
    }
    for name, kind, kw in _SCALAR_FIELDS:
        raw = row.get(name)
        if name in ("color_series", "gender"):
            rec[name] = [str(x)[:32] for x in (raw or [])][:8]
        elif name == "is_intimate":
            # 与 build_text_milvus_index 一致：存小写 "true"/"false"（expr 用 == "false" 过滤贴身）
            rec[name] = "true" if raw else "false"
        elif name == "up_time":
            # up_time 源是日期字符串（"2023-02-27 16:54:56"），不能用 int(float()) 否则
            # ValueError 落到 0（历史 bug：fila_sku_hybrid_vectors 全表 up_time=0 的根因）。
            rec[name] = up_time_to_epoch(raw)
        elif kind == "VARCHAR":
            if name == "season" and isinstance(raw, list):
                rec[name] = ",".join(str(x) for x in raw if x)[: kw["max_length"]]
            else:
                rec[name] = str(raw or "")[: kw["max_length"]]
        elif kind == "DOUBLE":
            try:
                rec[name] = float(raw or 0.0)
            except (TypeError, ValueError):
                rec[name] = 0.0
        elif kind == "INT64":
            try:
                rec[name] = int(float(raw or 0))
            except (TypeError, ValueError):
                rec[name] = 0
    return rec


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def main() -> None:
    parser = argparse.ArgumentParser(description="FILA Milvus hybrid 索引构建")
    parser.add_argument("--reset", action="store_true", help="删除并重建集合")
    parser.add_argument("--incremental", action="store_true", help="仅 search_text 签名变化时重算")
    parser.add_argument("--limit", type=int, default=0, help="仅前 N 条(smoke)")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--state-file", type=str, default="")
    args = parser.parse_args()

    cfg = load_yaml_config()
    mv = cfg.get("milvus") or {}
    if not mv.get("enabled"):
        raise SystemExit("milvus.enabled 为 false")
    from backend.config import (  # noqa: E402
        get_milvus_token,
        get_milvus_uri,
        restore_stashed_milvus_uri,
        stash_milvus_db_uri_before_pymilvus_import,
    )
    from backend.embedding_client import embed_text  # noqa: E402

    uri_env = str(mv.get("uri_env") or "FILA_MILVUS_URI")
    stash_milvus_db_uri_before_pymilvus_import(uri_env)
    try:
        _import_pymilvus()
    finally:
        restore_stashed_milvus_uri()
    if MilvusClient is None:
        raise SystemExit("pymilvus 不可用，见 requirements.txt")

    uri = get_milvus_uri(cfg)
    token = get_milvus_token(cfg)
    col_name = str(
        (mv.get("collections") or {}).get("sku_hybrid_vectors") or "fila_sku_hybrid_vectors"
    )
    dim = int((cfg.get("embedding") or {}).get("dimensions") or 1024)
    embedding_model = str((cfg.get("embedding") or {}).get("model") or "")

    state_path = (
        Path(args.state_file).expanduser().resolve() if args.state_file.strip() else DEFAULT_STATE_PATH
    )
    state = load_state(state_path)
    state["milvus"].setdefault("sku_hybrid_vectors", {})

    proc = ROOT / (cfg.get("paths") or {}).get("processed_dir", "data/processed")
    skus_path = proc / "skus.jsonl"
    if not skus_path.is_file():
        raise SystemExit(f"缺少 {skus_path}，先跑 scripts/build_catalog.py")

    client = MilvusClient(uri=uri, token=token or None)

    if args.reset:
        clear_milvus_bucket(state, "sku_hybrid_vectors")
        if client.has_collection(col_name):
            client.drop_collection(col_name)
            logger.info("Dropped: %s", col_name)
    if not client.has_collection(col_name):
        create_hybrid_collection(client, col_name, dim, uri)

    prior_sigs = state["milvus"]["sku_hybrid_vectors"]
    batch: list[dict[str, Any]] = []
    ok = skip = 0
    file_sigs: dict[str, str] = {}
    first_vec: list[float] | None = None
    rows = list(iter_jsonl(skus_path))
    if args.limit > 0:
        rows = rows[: args.limit]
    for idx, row in enumerate(rows, 1):
        sku_id = str(row.get("sku_id") or "").strip()
        if not sku_id:
            continue
        kw_text = build_keyword_text(row)
        sig = milvus_text_row_signature(kw_text, dimensions=dim, embedding_model=embedding_model)
        file_sigs[sku_id] = sig
        if args.incremental and prior_sigs.get(sku_id) == sig:
            skip += 1
            continue
        sem_text = build_semantic_text(row)
        vec = embed_text(sem_text)
        if not vec or len(vec) != dim:
            skip += 1
            continue
        if first_vec is None:
            first_vec = vec
        batch.append(build_insert_row(row, vec, dim))
        if len(batch) >= args.batch_size:
            client.insert(col_name, batch)
            ok += len(batch)
            batch.clear()
            logger.info("[%d] inserted=%d skip=%d", idx, ok, skip)
        time.sleep(0)
    if batch:
        client.insert(col_name, batch)
        ok += len(batch)
    client.flush(col_name)

    if not args.limit:
        state["milvus"]["sku_hybrid_vectors"] = file_sigs
        save_state(state, state_path)

    logger.info("hybrid index done: col=%s inserted=%d skipped=%d uri=%s", col_name, ok, skip, uri)
    if args.limit and first_vec is None:
        raise SystemExit("TEST: 无向量写入，检查 ARK_API_KEY 与网络")
    print(f"\nhybrid 索引构建结束。collection={col_name} inserted={ok} skip={skip}\n  uri={uri}")


if __name__ == "__main__":
    main()
