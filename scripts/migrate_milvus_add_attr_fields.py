#!/usr/bin/env python3
"""迁移 Milvus 三个 collection：补 layer/coverage/length_class/is_intimate/scene_domain/age 标量字段。

背景
----
Part A 给三个 builder 的 schema 加了若干标量字段，但线上 collection 是用**旧代码**
``--reset`` 重建的（06-25 23:49 启动，早于 09:33 的 schema 改动），schema 里没有这些
字段，导致 Part B 召回 expr（``is_intimate == "false"``、``scene_domain in [...]`` 等）
抛 ``field not exist``。

向量是昨晚花 ~10h 重新 embed 好的，不该再 ``--reset`` 重跑一遍。本脚本把现有向量 +
标量**读出落盘** → drop → 用新 schema 重建 → 连同新属性字段一起 upsert 回去，
**零 embedding API 调用**。

安全
----
两步分离：
  1. ``--dump``：只读，把每个 collection 全量（pk + 向量 + 标量）写到
     ``data/processed/milvus_dump_<coll>.jsonl``。不碰线上数据。
  2. ``--apply``：读 dump 文件 → drop collection → 用 builder 的新 schema 重建 →
     upsert（每条补上 skus.jsonl 里的 layer/coverage/length_class/is_intimate/scene_domain）。
upsert 幂等；--apply 崩溃后重跑 ``--apply`` 即可（dump 已落盘，drop/重建/upsert 幂等）。

用法
----
    cd fila_agent_html && export PYTHONPATH="$(pwd)"
    python scripts/migrate_milvus_add_attr_fields.py --dump                 # 先落盘
    python scripts/migrate_milvus_add_attr_fields.py --dump --collection fila_sku_vectors  # 单个
    python scripts/migrate_milvus_add_attr_fields.py --apply                # 重建+回灌
    python scripts/migrate_milvus_add_attr_fields.py --apply --collection fila_sku_vectors
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pymilvus import MilvusClient  # noqa: E402

from backend.config import (  # noqa: E402
    get_milvus_token,
    get_milvus_uri,
    load_config,
)
from backend.intent.color_series_mapper import map_color_to_series_list  # noqa: E402
from scripts.build_complementary_vectors import create_collection as create_complementary  # noqa: E402
from scripts.build_fila_milvus_multimodal_index import create_sku_collection as create_multimodal  # noqa: E402
from scripts.build_text_milvus_index import create_sku_text_collection as create_text  # noqa: E402
from scripts.etl_common import up_time_to_epoch  # noqa: E402

DUMP_DIR = ROOT / "data" / "processed"
SKUS_JSONL = ROOT / "data" / "processed" / "skus.jsonl"

# 每个 collection 的迁移配置：
#   pk: 主键字段；vec: 向量字段；dim: 维度
#   scalars: 需要原样带回的非主键标量字段（不含 sku_id，sku_id 单列便于按货号补属性）
#   create: 用新 schema 重建的函数 (client, name) -> None
COLLECTIONS: list[dict[str, Any]] = [
    {
        "name": "fila_sku_vectors",
        "pk": "doc_id",
        "vec": "product_vector",
        "dim": 1024,
        "scalars": [
            "sku_id", "spu_id", "product_name", "product_image",
            "color_series", "category_l2", "role", "gender", "season",
            "sku_id_for_group",
        ],
        # 从 skus.jsonl 补的属性字段；须与 create_* 的新 schema 字段完全对齐
        # （三个 collection 的新 schema 均含 modeling+price）
        "attr_fields": [
            "layer", "coverage", "length_class", "is_intimate", "scene_domain", "series",
            "group_brand", "modeling", "price", "age", "up_time", "color_name", "color_series",
            "id_goods",
        ],
        "create": lambda client, name, cfg: create_multimodal(client, name, 1024),
    },
    {
        "name": "fila_sku_text_vectors",
        "pk": "sku_id",
        "vec": "text_vector",
        "dim": 1024,
        "scalars": [
            "spu_id", "product_name", "product_intro",
            "color_series", "category_l2", "role", "gender", "season",
        ],
        "attr_fields": [
            "layer", "coverage", "length_class", "is_intimate", "scene_domain", "series",
            "group_brand", "modeling", "price", "age", "up_time", "color_name", "color_series",
            "id_goods",
        ],
        "create": lambda client, name, cfg: create_text(
            client, name, 1024, get_milvus_uri(cfg), cfg.get("milvus") or {}, "text_vector",
        ),
    },
    {
        "name": "fila_sku_complementary_vectors",
        "pk": "sku_id",
        "vec": "complementary_vector",
        "dim": 128,
        "scalars": [
            "spu_id", "role", "category_l2", "gender", "season", "color_series",
        ],
        "attr_fields": [
            "layer", "coverage", "length_class", "is_intimate", "scene_domain", "series",
            "group_brand", "modeling", "price", "age", "up_time", "color_name", "color_series",
            "id_goods",
        ],
        "create": lambda client, name, cfg: create_complementary(
            client, name, get_milvus_uri(cfg), cfg.get("milvus") or {},
        ),
    },
]


def _dump_path(name: str) -> Path:
    return DUMP_DIR / f"milvus_dump_{name}.jsonl"


def _to_float(v: Any) -> float:
    """price 字段对齐 Milvus DOUBLE（非 nullable），缺失/非法一律 0.0。"""
    try:
        if v is None or v == "":
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _to_jsonable(v: Any) -> Any:
    """Milvus 返回的 protobuf 容器 / numpy 标量转成 JSON 可序列化的原生类型。"""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (list, tuple)):
        return [_to_jsonable(x) for x in v]
    # numpy 标量
    if hasattr(v, "item") and not isinstance(v, (list, tuple)):
        try:
            return v.item()
        except Exception:
            pass
    # protobuf RepeatedScalarContainer 等：hasattr(__iter__) 可能 False 但 list() 可用
    try:
        return [_to_jsonable(x) for x in v]
    except Exception:
        return str(v)


def _chunks(ids: list[str], size: int):
    for i in range(0, len(ids), size):
        yield ids[i : i + size]


def _fetch_existing_pks(
    client: MilvusClient,
    collection: str,
    pk_field: str,
    candidates: set[str],
    chunk_size: int = 500,
) -> set[str]:
    """分批查询 candidates 中已存在于 Milvus 集合的主键。

    用于 --apply 续跑时跳过已 upsert 的行。查询失败返回空集（退化为全量 upsert，幂等安全）。
    """
    if not candidates:
        return set()
    clist = sorted(candidates)
    existing: set[str] = set()
    try:
        for part in _chunks(clist, chunk_size):
            expr = f'{pk_field} in [' + ", ".join(f'"{d}"' for d in part) + "]"
            res = client.query(
                collection_name=collection,
                filter=expr,
                output_fields=[pk_field],
                limit=len(part),
            )
            for r in res:
                v = r.get(pk_field) if isinstance(r, dict) else getattr(r, pk_field, None)
                if v is not None:
                    existing.add(str(v))
    except Exception as exc:
        print(f"  查询已有 {pk_field} 失败（忽略，按全量处理）: {exc}", file=sys.stderr)
        return set()
    return existing


def _connect(cfg: dict[str, Any]) -> MilvusClient:
    uri = get_milvus_uri(cfg)
    token = get_milvus_token(cfg)
    return MilvusClient(uri=uri, token=token or None)


def dump_one(client: MilvusClient, coll: dict[str, Any]) -> int:
    name = coll["name"]
    pk = coll["pk"]
    vec = coll["vec"]
    out_fields = [pk, vec] + [f for f in coll["scalars"] if f != pk]
    # 去重保序（pk 可能也在 scalars 里）
    seen: set[str] = set()
    out_fields = [f for f in out_fields if not (f in seen or seen.add(f))]

    out_path = _dump_path(name)
    n = 0
    it = client.query_iterator(
        collection_name=name,
        output_fields=out_fields,
        batch_size=2000,
    )
    with out_path.open("w", encoding="utf-8") as f:
        while True:
            batch = it.next()
            if not batch:
                break
            for row in batch:
                row = {k: _to_jsonable(v) for k, v in row.items()}
                # 向量是 list[float]，直接 json 化；标量原样
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                n += 1
    return n


def load_attr_index() -> dict[str, dict[str, Any]]:
    """sku_id -> {layer, coverage, length_class, is_intimate, scene_domain, age, up_time, color_name, color_series}（来自 skus.jsonl）。

    up_time 存 epoch 秒（int），与 Milvus INT64 字段对齐；其余为 str。
    color_name 与 ES 对齐（具体色名），便于 Milvus 侧按色名过滤。
    color_series 优先取 skus.jsonl 已落盘值（build_sku_record 派生为 list），为空则用
    map_color_to_series_list(color_name/attr_name) 现场派生——与 ES 构建及 enrich_sku_attributes
    同源，确保迁移回灌的 color_series 与重建索引一致，覆盖 dump 里线上旧标量值。
    """
    idx: dict[str, dict[str, Any]] = {}
    with SKUS_JSONL.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            sid = str(d.get("sku_id") or "").strip()
            if not sid:
                continue
            color_name = str(d.get("color_name") or d.get("attr_name") or "")[:64]
            raw_cs = d.get("color_series")
            if isinstance(raw_cs, list):
                cs_list = [str(x).strip() for x in raw_cs if str(x).strip()]
            elif isinstance(raw_cs, str) and raw_cs.strip():
                cs_list = [raw_cs.strip()]
            else:
                cs_list = []
            if not cs_list:
                cs_list = map_color_to_series_list(color_name)
            # Milvus ARRAY max_capacity=8, element max_length=32
            cs_list = [s[:32] for s in cs_list][:8]
            idx[sid] = {
                "layer": str(d.get("layer") or "")[:16],
                "coverage": str(d.get("coverage") or "")[:16],
                "length_class": str(d.get("length_class") or "")[:16],
                "is_intimate": "true" if d.get("is_intimate") else "false",
                "scene_domain": str(d.get("scene_domain") or "")[:32],
                "series": str(d.get("series") or "")[:64],
                "group_brand": str(d.get("group_brand") or "")[:64],
                "modeling": str(d.get("modeling") or "")[:16],
                "price": _to_float(d.get("price")),
                "age": str(d.get("age") or "")[:16],
                "up_time": up_time_to_epoch(d.get("up_time")),
                "color_name": color_name,
                "color_series": cs_list,
                "id_goods": int(d.get("id_goods") or d.get("goods_id") or 0),
            }
    return idx


def apply_one(
    client: MilvusClient,
    coll: dict[str, Any],
    cfg: dict[str, Any],
    *,
    skip_existing: bool = False,
) -> tuple[int, int, int]:
    name = coll["name"]
    pk = coll["pk"]
    vec = coll["vec"]
    dump_path = _dump_path(name)
    if not dump_path.is_file():
        raise SystemExit(f"缺少 dump 文件 {dump_path}，请先 --dump")

    attrs = load_attr_index()

    # 1. drop + 用新 schema 重建（续跑模式跳过 drop，复用已建好的新 schema collection）
    if skip_existing:
        if not client.has_collection(name):
            coll["create"](client, name, cfg)
            print(f"  created (new schema): {name}")
        else:
            print(f"  skip drop, reuse existing: {name}")
    else:
        if client.has_collection(name):
            client.drop_collection(name)
            print(f"  dropped: {name}")
        coll["create"](client, name, cfg)
        print(f"  recreated (new schema): {name}")

    # 续跑：先扫 dump 拿到所有候选 pk，查 Milvus 已存在的，跳过
    existing_pks: set[str] = set()
    if skip_existing:
        candidates: set[str] = set()
        with dump_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                v = row.get(pk)
                if v is not None:
                    candidates.add(str(v))
        existing_pks = _fetch_existing_pks(client, name, pk, candidates)
        print(
            f"  skip-existing: {len(existing_pks)} / {len(candidates)} 已存在，将跳过",
        )

    # 2. 从 dump 读回 + 补属性字段 + upsert
    attr_fields = coll.get("attr_fields", [])
    # 行只保留新 schema 存在的字段：pk + vec + scalars + attr_fields。
    # 否则 dump 里线上旧标量可能多出新 schema 没有的字段，upsert 会报 "extra field"。
    allowed = {pk, vec, *coll["scalars"], *attr_fields}

    # 缺属性时用的兜底值（非 nullable 字段不能给 None）
    default_attr = {f: (0.0 if f == "price" else 0 if f in ("up_time", "id_goods") else []) if f in ("price", "up_time", "id_goods", "color_series") else "" for f in attr_fields}
    default_attr["is_intimate"] = "false"

    batch: list[dict[str, Any]] = []
    BATCH = 1000
    total = 0
    skipped_existing = 0
    missing_attr = 0

    def flush() -> None:
        nonlocal batch
        if batch:
            client.upsert(collection_name=name, data=batch)
            batch = []

    with dump_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            pkv = str(row.get(pk) or "")
            if skip_existing and pkv and pkv in existing_pks:
                skipped_existing += 1
                continue
            sid = str(row.get("sku_id") or "")
            a = attrs.get(sid)
            if a is None:
                missing_attr += 1
                a = default_attr
            # 丢掉 dump 行里新 schema 没有的多余字段
            row = {k: v for k, v in row.items() if k in allowed}
            # 补/覆盖属性字段（覆盖 dump 里线上旧标量值，统一以 skus.jsonl 为准）
            for f in attr_fields:
                row[f] = a.get(f, default_attr[f])
            batch.append(row)
            total += 1
            if len(batch) >= BATCH:
                flush()
    flush()
    if skip_existing:
        print(f"  skipped(existing)={skipped_existing}")
    return total, missing_attr, skipped_existing


def main() -> int:
    parser = argparse.ArgumentParser(
        description="迁移 Milvus collection 补 layer/coverage/length_class/is_intimate/scene_domain（零重 embed）",
    )
    parser.add_argument("--dump", action="store_true", help="只把线上数据落盘到 jsonl，不改动线上")
    parser.add_argument("--apply", action="store_true", help="读 dump → drop → 新 schema 重建 → upsert")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="apply 续跑：不 drop，查询已存在主键并跳过，仅 upsert 缺失的行",
    )
    parser.add_argument("--collection", type=str, default=None, help="只处理指定 collection")
    args = parser.parse_args()

    if not args.dump and not args.apply:
        parser.error("至少指定 --dump 或 --apply")

    cfg = load_config()
    client = _connect(cfg)

    targets = [c for c in COLLECTIONS if not args.collection or c["name"] == args.collection]
    if not targets:
        print(f"未找到 collection: {args.collection}", file=sys.stderr)
        return 1

    if args.dump:
        for coll in targets:
            n = dump_one(client, coll)
            print(f"[dump] {coll['name']}: {n} rows -> {_dump_path(coll['name'])}")

    if args.apply:
        attrs = load_attr_index()
        print(f"[apply] skus.jsonl 属性索引: {len(attrs)} 条")
        for coll in targets:
            total, missing, skipped = apply_one(
                client, coll, cfg, skip_existing=args.skip_existing,
            )
            print(f"[apply] {coll['name']}: upsert={total}, 缺属性(用默认空)={missing}, skipped(existing)={skipped}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
