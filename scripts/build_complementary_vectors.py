#!/usr/bin/env python3
"""FILA SKU 互补向量索引（outfit-transformer embed_items → Milvus）。

使用 outfit-transformer serve 的 /embed_items 端点为每个 SKU 生成 128 维互补
嵌入向量，并写入 Milvus 新 collection fila_sku_complementary_vectors。

用法（在 fila_agent_html 目录下）::

  source .venv/bin/activate
  export PYTHONPATH="$(pwd)"
  python3 scripts/build_complementary_vectors.py \\
      --serve-url http://10.213.148.68:32465 \\
      [--reset] [--skip-existing] [--batch-size 16] [--test-limit 100]

  --reset         drop 并重建集合（全量重建）。
  --skip-existing 不 drop，按 sku_id 跳过已索引项，只补缺失（续跑增量）。
                  两者同传时 reset 生效（重建后集合为空 → 等价全量）。

依赖：
  - outfit-transformer serve 服务运行中（提供 /embed_items 端点）
  - Milvus 可用（cloud 或 local，同现有文本向量索引）
  - data/processed/skus.jsonl 存在
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Iterator, List

import httpx
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("build_complementary_vectors")

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.yaml"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.etl_common import up_time_to_epoch

DataType = None  # type: ignore
MilvusClient = None  # type: ignore


def _import_pymilvus() -> None:
    global DataType, MilvusClient
    if MilvusClient is not None:
        return
    from pymilvus import DataType as _DT, MilvusClient as _MC
    DataType = _DT
    MilvusClient = _MC


def load_yaml_config() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def _chunks(seq: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _fetch_existing_sku_ids(
    client: Any,
    collection: str,
    candidates: set[str],
    chunk_size: int = 500,
) -> set[str]:
    """分批查询 candidates 中已存在于互补向量集合的 sku_id。

    用于 ``--skip-existing`` 续跑时跳过已索引 SKU，省掉重复 embedding 调用。
    查询失败返回空集（退化为全量 embed+upsert，幂等安全）。
    """
    if not candidates:
        return set()
    clist = sorted(candidates)
    existing: set[str] = set()
    try:
        for part in _chunks(clist, chunk_size):
            expr = "sku_id in [" + ", ".join(f'"{c}"' for c in part) + "]"
            res = client.query(
                collection_name=collection,
                filter=expr,
                output_fields=["sku_id"],
                limit=len(part),
            )
            for r in res:
                v = r.get("sku_id") if isinstance(r, dict) else getattr(r, "sku_id", None)
                if v is not None:
                    existing.add(str(v))
    except Exception as exc:
        logger.warning("查询已索引 sku_id 失败（忽略，按全量处理）: %s", exc)
        return set()
    return existing


EMBED_DIM = 128
VECTOR_FIELD = "complementary_vector"


def _to_float(v: Any) -> float:
    """price 字段对齐 Milvus DOUBLE（非 nullable），缺失/非法一律 0.0。"""
    try:
        if v is None or v == "":
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _vector_index_type_and_params(uri: str, mv: dict[str, Any]) -> tuple[str, dict]:
    from backend.config import is_milvus_lite_local_uri

    metric = str(mv.get("metric_type") or "COSINE")
    if is_milvus_lite_local_uri(uri):
        return "AUTOINDEX", {"metric_type": metric}
    ip = mv.get("index_params") or {"M": 32, "efConstruction": 200}
    return str(mv.get("index_type") or "HNSW"), {
        "metric_type": metric,
        "params": ip,
    }


def create_collection(
    client: Any,
    name: str,
    uri: str,
    mv: dict[str, Any],
) -> None:
    _import_pymilvus()
    schema = client.create_schema()
    schema.add_field(
        "sku_id",
        DataType.VARCHAR,
        max_length=64,
        is_primary=True,
        auto_id=False,
    )
    schema.add_field(VECTOR_FIELD, DataType.FLOAT_VECTOR, dim=EMBED_DIM)
    schema.add_field("spu_id", DataType.VARCHAR, max_length=32)
    schema.add_field("role", DataType.VARCHAR, max_length=32)
    schema.add_field("category_l2", DataType.VARCHAR, max_length=64)
    schema.add_field(
        "gender",
        DataType.ARRAY,
        element_type=DataType.VARCHAR,
        max_length=32,
        max_capacity=8,
    )
    schema.add_field("season", DataType.VARCHAR, max_length=256)
    schema.add_field("color_series", DataType.ARRAY, element_type=DataType.VARCHAR, max_length=32, max_capacity=8)
    schema.add_field("color_name", DataType.VARCHAR, max_length=64)
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
    client.create_collection(name, schema=schema)
    idx = client.prepare_index_params()
    index_type, extra = _vector_index_type_and_params(uri, mv)
    metric = extra.get("metric_type", "COSINE")
    params = extra.get("params") or {}
    idx.add_index(
        VECTOR_FIELD,
        index_type=index_type,
        index_name="sku_comp_vec_idx",
        metric_type=metric,
        params=params,
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
    logger.info("Created collection: %s (index=%s)", name, index_type)


def embed_items_batch(
    client: httpx.Client,
    endpoint: str,
    items: list[dict[str, str]],
    timeout: httpx.Timeout,
    max_retries: int = 3,
    backoff_base: float = 2.0,
) -> list[list[float] | None] | None:
    """Call serve /embed_items for a batch of items, with retry + bisection.

    失败策略（目的是把 timeout/网络抖动造成的丢项降到最低）：

    - 瞬时错误（超时 / 网络层 / 5xx）按指数退避重试 ``max_retries`` 次。
    - 4xx 不重试（请求本身有缺陷，重试无益），直接进入二分。
    - 仍失败时对 batch 二分：两半各自重试，尽量抢救可嵌入子集。
      返回值长度恒等于 ``len(items)``，失败子项以 ``None`` 占位，
      由调用方逐项跳过（``if not emb`` 已覆盖 None）。
    - 整批（含叶子单条）全失败时返回 ``None``。
    """
    last_err = "no attempt made"
    for attempt in range(max_retries + 1):
        try:
            resp = client.post(endpoint, json={"items": items}, timeout=timeout)
            resp.raise_for_status()
            embs = resp.json().get("embeddings")
            if embs is not None and len(embs) == len(items):
                return embs
            last_err = (
                f"embeddings count {len(embs) if embs is not None else 'None'} "
                f"!= items {len(items)}"
            )
        except httpx.HTTPStatusError as exc:
            last_err = f"HTTP {exc.response.status_code}: {exc}"
            if exc.response.status_code < 500:
                # 4xx 重试无益，直接二分定位坏项
                break
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        if attempt < max_retries:
            wait = backoff_base * (2 ** attempt)
            logger.warning(
                "embed_items attempt %d/%d 失败 (%s)，%.1fs 后重试 (batch=%d)",
                attempt + 1, max_retries + 1, last_err, wait, len(items),
            )
            time.sleep(wait)
        else:
            logger.warning(
                "embed_items 已重试 %d 次仍失败: %s", max_retries + 1, last_err,
            )

    # 二分抢救：失败 batch 拆半各自重试，None 占位保证长度对齐
    if len(items) > 1:
        mid = len(items) // 2
        logger.info(
            "二分 batch %d → %d + %d 尝试部分抢救", len(items), mid, len(items) - mid,
        )
        left = embed_items_batch(client, endpoint, items[:mid], timeout, max_retries, backoff_base)
        right = embed_items_batch(client, endpoint, items[mid:], timeout, max_retries, backoff_base)
        if left is None and right is None:
            return None
        if left is None:
            left = [None] * mid
        if right is None:
            right = [None] * (len(items) - mid)
        return list(left) + list(right)
    return None


def main():
    parser = argparse.ArgumentParser(description="Build complementary vectors Milvus index")
    parser.add_argument("--serve-url", type=str, required=True,
                        help="outfit-transformer serve URL (e.g. http://host:8080)")
    parser.add_argument("--reset", action="store_true", help="Drop and recreate collection")
    parser.add_argument("--skip-existing", action="store_true",
                        help="跳过已索引 SKU（按 sku_id 检测），只 embed 并 upsert 缺失项；"
                             "与 --reset 同时给出时按全量处理（reset 后集合为空）")
    parser.add_argument("--batch-size", type=int, default=16,
                        help="Batch size for embed_items calls")
    parser.add_argument("--test-limit", type=int, default=0,
                        help="Limit number of SKUs (0 = all)")
    parser.add_argument("--timeout", type=int, default=120,
                        help="HTTP read timeout per batch (seconds)；瞬时超时会自动重试")
    parser.add_argument("--max-retries", type=int, default=3,
                        help="每批 embed_items 失败后的重试次数（指数退避）")
    parser.add_argument("--backoff-base", type=float, default=2.0,
                        help="重试退避基数（秒），实际等待 base*2^attempt")
    args = parser.parse_args()

    _import_pymilvus()
    if MilvusClient is None:
        logger.error("pymilvus not installed")
        sys.exit(1)

    cfg = load_yaml_config()
    mv = cfg.get("milvus") or {}

    from backend.config import get_milvus_uri, get_milvus_token

    uri = get_milvus_uri(cfg)
    token = get_milvus_token(cfg)
    if not uri:
        logger.error("Milvus URI not configured")
        sys.exit(1)

    collection_name = (mv.get("collections") or {}).get(
        "sku_complementary_vectors", "fila_sku_complementary_vectors",
    )

    client = MilvusClient(uri=uri, token=token or None)

    if args.reset:
        if client.has_collection(collection_name):
            client.drop_collection(collection_name)
            logger.info("Dropped collection: %s", collection_name)

    if not client.has_collection(collection_name):
        create_collection(client, collection_name, uri, mv)

    # Load SKU data
    proc = ROOT / (cfg.get("paths") or {}).get("processed_dir", "data/processed")
    skus_path = proc / "skus.jsonl"
    if not skus_path.is_file():
        logger.error("Missing %s", skus_path)
        sys.exit(1)

    from backend.empty_image_urls import sku_has_empty_tryon_image
    from backend.intent.sku_attributes import enrich_sku_attributes

    sku_rows: list[dict[str, Any]] = []
    for row in iter_jsonl(skus_path):
        sku_id = str(row.get("sku_id") or "").strip()
        if not sku_id:
            continue
        if sku_has_empty_tryon_image(row):
            continue
        # 兜底推导结构化属性（jsonl 理论上已有，缺失则按 title+category_l2 实时推导）
        enrich_sku_attributes(row)
        image_url = str(row.get("tryon_image") or "").strip()
        if not image_url:
            images = row.get("images") or []
            if images:
                image_url = str(images[0] if isinstance(images[0], str) else images[0].get("url", ""))
        if not image_url:
            continue
        sku_rows.append(row)

    if args.test_limit > 0:
        sku_rows = sku_rows[:args.test_limit]

    skipped_existing = 0
    if args.skip_existing and not args.reset and client.has_collection(collection_name):
        candidates = {str(r.get("sku_id") or "") for r in sku_rows if r.get("sku_id")}
        existing = _fetch_existing_sku_ids(client, collection_name, candidates)
        before = len(sku_rows)
        sku_rows = [r for r in sku_rows if str(r.get("sku_id") or "") not in existing]
        skipped_existing = before - len(sku_rows)
        logger.info(
            "skip-existing: %d / %d 已索引，跳过；待 embed %d",
            skipped_existing, before, len(sku_rows),
        )

    logger.info("Total SKUs to process: %d", len(sku_rows))

    # 复用单例 httpx.Client：连接池保活，避免每批重新 TCP/TLS 握手；
    # 读超时给足（模型在重载下单批可能数十秒），连接/池等待用短超时快速失败。
    embed_endpoint = f"{args.serve_url.rstrip('/')}/embed_items"
    embed_timeout = httpx.Timeout(args.timeout, connect=10.0, write=30.0, pool=15.0)
    embed_client = httpx.Client(
        timeout=embed_timeout,
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
        headers={"Content-Type": "application/json"},
    )

    ok = 0
    skip = 0
    t0 = time.time()

    for batch_start in range(0, len(sku_rows), args.batch_size):
        batch = sku_rows[batch_start : batch_start + args.batch_size]

        items_payload = []
        for row in batch:
            image_url = str(row.get("tryon_image") or "").strip()
            if not image_url:
                images = row.get("images") or []
                if images:
                    image_url = str(images[0] if isinstance(images[0], str) else images[0].get("url", ""))
            description = str(row.get("title") or "").strip()
            # 传 sku_id 命中 serve 侧预计算的 complementary embedding 快路径
            # （跳过翻译/下载图/CLIP 编码），避免慢路径整批超过读超时被永久 skip。
            items_payload.append({
                "sku_id": str(row.get("sku_id") or ""),
                "image_url": image_url,
                "description": description,
            })

        embeddings = embed_items_batch(
            embed_client, embed_endpoint, items_payload,
            embed_timeout, args.max_retries, args.backoff_base,
        )
        if not embeddings or len(embeddings) != len(batch):
            logger.warning(
                "Batch %d-%d: embed_items returned %s embeddings, expected %d, skipping",
                batch_start, batch_start + len(batch),
                len(embeddings) if embeddings else 0,
                len(batch),
            )
            skip += len(batch)
            continue

        milvus_data: list[dict[str, Any]] = []
        for row, emb in zip(batch, embeddings):
            if not emb or len(emb) != EMBED_DIM:
                skip += 1
                continue
            sku_id = str(row.get("sku_id") or "")
            milvus_data.append({
                "sku_id": sku_id,
                VECTOR_FIELD: emb,
                "spu_id": str(row.get("spu_id") or "")[:32],
                "role": str(row.get("role") or "")[:32],
                "category_l2": str(row.get("category_l2") or "")[:64],
                "gender": [str(x)[:32] for x in row["gender"]] if isinstance(row.get("gender"), list) else ([str(row.get("gender"))[:32]] if row.get("gender") else []),
                "season": str(row.get("season") or "")[:256],
                "color_series": [str(x)[:32] for x in (row.get("color_series") or []) if str(x).strip()][:8],
                "color_name": str(row.get("color_name") or row.get("attr_name") or "")[:64],
                "layer": str(row.get("layer") or "")[:16],
                "coverage": str(row.get("coverage") or "")[:16],
                "length_class": str(row.get("length_class") or "")[:16],
                "is_intimate": "true" if row.get("is_intimate") else "false",
                "scene_domain": str(row.get("scene_domain") or "")[:32],
                "series": str(row.get("series") or "")[:64],
                "group_brand": str(row.get("group_brand") or "")[:64],
                "modeling": str(row.get("modeling") or "")[:16],
                "price": _to_float(row.get("price")),
                "age": str(row.get("age") or "")[:16],
                "up_time": up_time_to_epoch(row.get("up_time")),
                "id_goods": int(row.get("id_goods") or row.get("goods_id") or 0),
            })

        if milvus_data:
            client.upsert(collection_name=collection_name, data=milvus_data)
            ok += len(milvus_data)

        elapsed = time.time() - t0
        logger.info(
            "Progress: %d/%d (ok=%d, skip=%d, elapsed=%.1fs)",
            min(batch_start + len(batch), len(sku_rows)),
            len(sku_rows),
            ok, skip, elapsed,
        )

    elapsed = time.time() - t0
    logger.info(
        "Done: total=%d, ok=%d, skip=%d, skipped_existing=%d, elapsed=%.1fs",
        len(sku_rows), ok, skip, skipped_existing, elapsed,
    )

    # Verify
    if ok > 0 and embeddings:
        sample_vec = embeddings[0]
        logger.info("Verifying search ...")
        try:
            res = client.search(
                collection_name=collection_name,
                data=[sample_vec],
                anns_field=VECTOR_FIELD,
                limit=3,
                output_fields=["sku_id"],
            )
            for i, h in enumerate(res[0] if res else [], 1):
                ent = h.get("entity", h)
                dist = h.get("distance", getattr(h, "distance", None))
                logger.info("  #%d sku_id=%s dist=%s", i, ent.get("sku_id"), dist)
        except Exception:
            logger.exception("Verify search failed")

    embed_client.close()


if __name__ == "__main__":
    main()
