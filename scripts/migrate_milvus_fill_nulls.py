#!/usr/bin/env python3
"""不 reset，原地 upsert 补 Milvus 空值属性。

背景
----
audit_index_nulls.py 实测三个 SKU 集合属性大面积空/0：
  - age 在三集合 100% 空（源 skus.jsonl 已有 22.6% 有值）
  - up_time 在 hybrid 99.97% 是 0（int(float(日期串)) 历史 bug 残留）
  - group_brand sku_vectors/complementary 大面积空（源 94.8% 有值）
  - modeling/scene_domain/series/layer/coverage/length_class 在旧集合大量空

字段在 schema 里**已存在**（与 migrate_milvus_add_attr_fields 当初「字段不存在」场景不同），
故本脚本**不 drop、不重建 schema**，只 query_iterator 整行读出 → 合并最新源属性 →
upsert 整行覆盖（复用向量，零 embedding API 调用）。

机制约束
----
Milvus 无单列 UPDATE，upsert = 按主键删旧+插新，必须整行完整字段。故：
  - 向量字段（product_vector / complementary_vector / dense_vector）随整行带回，复用旧向量
  - hybrid 的 sparse_vector 是 BM25 Function 输出字段，upsert 不得带（排除）
  - hybrid 的 search_text 是 BM25 输入、非 nullable，用 dump 旧值带回（内容不变→sparse 重算幂等）
  - 字段集合须与 schema 严格对齐（白名单 = describe 的字段 - exclude）

补值策略
----
「以源为准、只补不破」：attr_fields 中，源 skus.jsonl 有值则覆盖 dump 旧值（含 up_time=0、
is_intimate 错误的纠错），源无值则保留 dump 旧值（不拿空覆盖有值）。

用法
----
    cd fila_agent_html && export PYTHONPATH="$(pwd)"
    python scripts/migrate_milvus_fill_nulls.py --dry-run                 # 全量预演，只统计不写
    python scripts/migrate_milvus_fill_nulls.py --dry-run --collection fila_sku_hybrid_vectors
    python scripts/migrate_milvus_fill_nulls.py --limit 200               # 小样本实测
    python scripts/migrate_milvus_fill_nulls.py                           # 全量补值
    python scripts/migrate_milvus_fill_nulls.py --collection fila_sku_vectors --batch-size 500

幂等：upsert 按 pk 幂等，崩了重跑即可。跑完用 audit_index_nulls.py 核验空率收敛。
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pymilvus import MilvusClient  # noqa: E402

from backend.config import (  # noqa: E402
    get_milvus_token,
    get_milvus_uri,
    load_config,
)
# 复用既有属性派生（与 ES/重建索引同源，已处理 color_series list 截断、
# is_intimate "true"/"false"、up_time epoch 转换、id_goods int 等）
from migrate_milvus_add_attr_fields import load_attr_index  # noqa: E402

# 要补值的属性字段（三集合 schema 均含）。season/gender 空率低且 ARRAY/VARCHAR
# 跨集合形态不同，暂不补；聚焦 audit 里空值率高的核心过滤/召回字段。
ATTR_FIELDS = [
    "layer", "coverage", "length_class", "is_intimate", "scene_domain", "series",
    "group_brand", "modeling", "price", "age", "up_time", "color_name", "color_series",
    "id_goods",
]

# pk: 主键；vec: 向量字段（随整行带回复用）；exclude: upsert/dump 都不带的字段
COLLECTIONS: list[dict[str, Any]] = [
    {
        "name": "fila_sku_vectors",
        "pk": "doc_id",
        "vec": "product_vector",
        "exclude": set(),
    },
    {
        "name": "fila_sku_complementary_vectors",
        "pk": "sku_id",
        "vec": "complementary_vector",
        "exclude": set(),
    },
    {
        "name": "fila_sku_hybrid_vectors",
        "pk": "sku_id",
        "vec": "dense_vector",
        # sparse_vector 是 BM25 Function 输出字段，不可写入
        "exclude": {"sparse_vector"},
    },
]


def _has_val(v: Any) -> bool:
    """attr 是否有可覆盖值。数值 0 视为空（up_time epoch=0 / id_goods=0 即未填）。"""
    if v is None:
        return False
    if isinstance(v, str):
        s = v.strip()
        return bool(s) and s.lower() not in ("null", "none", "nan")
    if isinstance(v, (list, tuple, set, dict)):
        return len(v) > 0
    if isinstance(v, (int, float)):
        return v != 0
    return True


def _describe_fields(client: MilvusClient, name: str) -> list[str]:
    desc = client.describe_collection(name)
    out: list[str] = []
    for f in desc.get("fields") or []:
        fn = f.get("name") or f.get("field_name")
        if fn:
            out.append(fn)
    return out


def _to_jsonable(v: Any) -> Any:
    """pymilvus query 返回的 numpy/标量归一为可 upsert 的纯 Python 值。"""
    try:
        import numpy as np  # type: ignore
    except ImportError:
        np = None  # type: ignore
    if np is not None and isinstance(v, np.generic):
        return v.item()
    if np is not None and isinstance(v, np.ndarray):
        return v.tolist()
    return v


def migrate_one(
    client: MilvusClient,
    coll: dict[str, Any],
    attrs: dict[str, dict[str, Any]],
    *,
    dry_run: bool,
    batch_size: int,
    limit: int,
) -> None:
    name = coll["name"]
    exclude = coll["exclude"]
    pk = coll["pk"]
    if not client.has_collection(name):
        print(f"[skip] {name} 不存在")
        return
    fields = _describe_fields(client, name)
    out_fields = [f for f in fields if f not in exclude]
    eff_attr = [f for f in ATTR_FIELDS if f in fields]
    print(f"\n=== {name} ===")
    print(f"schema 字段 {len(fields)} | dump/upsert 字段 {len(out_fields)} | 补值字段 {len(eff_attr)}: {eff_attr}")
    if not eff_attr:
        print("  无可补字段，跳过")
        return

    attrs_total = len(attrs)
    print(f"源属性索引 {attrs_total} 个 SKU")

    it = client.query_iterator(
        collection_name=name,
        output_fields=out_fields,
        batch_size=2000,
    )
    batch: list[dict[str, Any]] = []
    total = 0
    filled = Counter()  # dump 空 → attr 补
    covered = Counter()  # dump 有值 → attr 覆盖（纠错，如 up_time=0、is_intimate 错）
    no_sku_id = 0

    def flush() -> None:
        nonlocal batch
        if batch and not dry_run:
            client.upsert(collection_name=name, data=batch)
        batch.clear()

    while True:
        rows = it.next()
        if not rows:
            break
        for row in rows:
            total += 1
            # 归一 numpy 值（向量、标量都可能带 numpy 类型）
            row = {k: _to_jsonable(v) for k, v in row.items()}
            sid = str(row.get("sku_id") or "").strip()
            if not sid:
                no_sku_id += 1
            a = attrs.get(sid, {}) if sid else {}
            for f in eff_attr:
                av = a.get(f)
                if _has_val(av):
                    if not _has_val(row.get(f)):
                        filled[f] += 1
                    else:
                        covered[f] += 1
                    row[f] = av
            batch.append(row)
            if len(batch) >= batch_size:
                flush()
            if limit and total >= limit:
                flush()
                _report(name, total, filled, covered, no_sku_id, dry_run, limit_hit=True)
                return
    flush()
    _report(name, total, filled, covered, no_sku_id, dry_run, limit_hit=False)


def _report(name, total, filled, covered, no_sku_id, dry_run, *, limit_hit):
    tag = "DRY-RUN" if dry_run else "APPLIED"
    lim = f" (limit 命中，仅处理 {total} 行)" if limit_hit else ""
    print(f"[{tag}] {name}: 扫描 {total} 行{lim}")
    if no_sku_id:
        print(f"  无 sku_id 跳过属性补值: {no_sku_id} 行")
    if filled:
        print(f"  补空(空→有值):")
        for f, n in filled.most_common():
            print(f"    {f:<18} {n}")
    if covered:
        print(f"  纠错(有值→覆盖):")
        for f, n in covered.most_common():
            print(f"    {f:<18} {n}")
    if not filled and not covered:
        print("  无需补值")


def main() -> int:
    ap = argparse.ArgumentParser(description="不 reset 原地 upsert 补 Milvus 空值属性")
    ap.add_argument("--collection", help="只处理指定集合名（默认全部三个）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不 upsert")
    ap.add_argument("--batch-size", type=int, default=1000, help="upsert 批大小（默认 1000）")
    ap.add_argument("--limit", type=int, default=0, help="每集合最多处理 N 行（0=全量，测试用）")
    args = ap.parse_args()

    cfg = load_config()
    uri = get_milvus_uri(cfg)
    token = get_milvus_token(cfg)
    print(f"Milvus: {uri}")
    client = MilvusClient(uri=uri, token=token)

    attrs = load_attr_index()
    print(f"已加载源属性 {len(attrs)} 个 SKU（from data/processed/skus.jsonl）")

    targets = [c for c in COLLECTIONS if not args.collection or c["name"] == args.collection]
    if not targets:
        print(f"未匹配任何集合: {args.collection}")
        return 1
    for coll in targets:
        migrate_one(
            client, coll, attrs,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
            limit=args.limit,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
