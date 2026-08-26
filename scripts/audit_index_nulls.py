#!/usr/bin/env python3
"""审计 ES 与 Milvus 索引各字段的空值率，找出需要填补的属性。

对每个索引：抽样（默认全量 scan/分页 query）所有文档，逐字段统计
「空值」占比。空值口径：
  - str: "" / 仅空白 / "null"
  - list/array: []
  - 数值: None / 缺失（0 视为有效值，单独提示）
  - dict/object: {} / None
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import (
    create_elasticsearch_client,
    env_or_empty,
    get_elasticsearch_hosts,
    get_elasticsearch_indices,
    get_milvus_uri,
    get_milvus_token,
    load_config,
)

# ── 空值判定 ────────────────────────────────────────────────
def is_empty(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        s = v.strip()
        return s == "" or s.lower() in ("null", "none", "nan")
    if isinstance(v, (list, tuple, set, dict)):
        return len(v) == 0
    return False


def audit_es(cfg):
    from elasticsearch import Elasticsearch
    from elasticsearch.helpers import scan

    hosts = get_elasticsearch_hosts(cfg)
    es_cfg = cfg.get("elasticsearch") or {}
    user = env_or_empty(str(es_cfg.get("username_env") or ""))
    pwd = env_or_empty(str(es_cfg.get("password_env") or ""))
    client = create_elasticsearch_client(hosts, username=user, password=pwd, timeout_sec=60)
    if not client.ping():
        print("[ES] ping 失败")
        return
    indices = get_elasticsearch_indices(cfg)
    for key, name in indices.items():
        if not client.indices.exists(index=name):
            print(f"[ES] {key} ({name}) 不存在，跳过")
            continue
        print(f"\n=== ES 索引 {key}: {name} ===")
        # 用 aggregation 统计每个已知字段的空值数（避免全量 scan 开销）
        # 先取 mapping 拿到全部字段
        mapping = client.indices.get_mapping(index=name)
        props = (
            mapping.get(name, {})
            .get("mappings", {})
            .get("properties", {})
        )
        total = client.count(index=name, body={"query": {"match_all": {}}}).get("count", 0)
        print(f"文档总数: {total}")
        if total == 0:
            continue
        # 全量 scan 统计（精确，字段不算多）
        field_total = defaultdict(int)
        field_empty = defaultdict(int)
        seen_fields = set()
        for doc in scan(client, index=name, query={"query": {"match_all": {}}}, size=2000, _source=True):
            src = doc.get("_source") or {}
            for k, v in src.items():
                seen_fields.add(k)
                field_total[k] += 1
                if is_empty(v):
                    field_empty[k] += 1
        # 字段未出现在任何文档（缺失）= 全空
        for fname in props:
            if fname not in seen_fields:
                field_total[fname] = 0
                field_empty[fname] = total
        rows = []
        for fname in props:
            tot = field_total.get(fname, 0)
            emp = field_empty.get(fname, 0)
            # 缺失口径：未出现的文档也算空
            missing_docs = total - tot
            emp_total = emp + missing_docs
            rate = emp_total / total if total else 0
            rows.append((fname, emp_total, total, rate))
        rows.sort(key=lambda r: -r[3])
        print(f"{'字段':<28}{'空值数':>10}{'/总数':>10}{'空率':>10}")
        for fname, emp, tot, rate in rows:
            flag = "  <-- 需填补" if rate > 0 else ""
            print(f"{fname:<28}{emp:>10}{tot:>10}{rate:>9.1%}{flag}")


def _milvus_empty(v, dtype) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        s = v.strip()
        return s == "" or s.lower() in ("null", "none", "nan")
    if isinstance(v, (list, tuple, set)):
        return len(v) == 0
    # 数值 0 不算空
    return False


def audit_milvus(cfg):
    from pymilvus import MilvusClient

    uri = get_milvus_uri(cfg)
    token = get_milvus_token(cfg)
    print(f"\n[Milvus] uri={uri}")
    client = MilvusClient(uri=uri, token=token)
    collections = (cfg.get("milvus") or {}).get("collections") or {}
    # 跳过体积过大、与「空属性」无关的字段（向量、BM25 输入长文本）
    SKIP_FIELDS = {
        "search_text", "sparse_vector", "product_intro",
        "complementary_vector", "product_vector", "text_vector",
    }
    for key, name in collections.items():
        if not client.has_collection(name):
            print(f"[Milvus] {key} ({name}) 不存在，跳过")
            continue
        print(f"\n=== Milvus 集合 {key}: {name} ===")
        desc = client.describe_collection(name)
        fields = desc.get("fields") or []
        scalar_fields = []
        for f in fields:
            dtype = str(f.get("type") or f.get("dataType") or "")
            fname = f.get("name") or f.get("field_name") or ""
            if not fname or fname in SKIP_FIELDS:
                continue
            if "VECTOR" in dtype.upper():
                continue
            scalar_fields.append((fname, dtype))
        field_names = [f[0] for f in scalar_fields]
        try:
            stats = client.get_collection_stats(name)
            total = int(stats.get("row_count", 0) or 0)
        except Exception:
            total = 0
        print(f"行数(统计): {total}")
        field_total = defaultdict(int)
        field_empty = defaultdict(int)
        seen = 0
        # 分页 query：segcore 对单次返回字节数有上限，故小批量 + offset
        batch = 1000
        offset = 0
        while True:
            try:
                rows = client.query(
                    name,
                    filter="",
                    output_fields=field_names,
                    limit=batch,
                    offset=offset,
                )
            except Exception as e:
                # 仍超限 → 降到更小批次继续
                if batch > 200:
                    batch = max(200, batch // 2)
                    continue
                print(f"  query 失败(offset={offset}, batch={batch}): {e}")
                break
            if not rows:
                break
            for r in rows:
                seen += 1
                for fname, dtype in scalar_fields:
                    v = r.get(fname)
                    if v is None:
                        field_empty[fname] += 1
                    else:
                        field_total[fname] += 1
                        if _milvus_empty(v, dtype):
                            field_empty[fname] += 1
            offset += len(rows)
            if len(rows) < batch:
                break
            # 防御：抽到全量即止
            if total and seen >= total:
                break
        total_n = seen
        print(f"抽样行数: {total_n}")
        if total_n == 0:
            continue
        rows_out = []
        for fname, dtype in scalar_fields:
            emp = field_empty.get(fname, 0)
            rate = emp / total_n if total_n else 0
            rows_out.append((fname, dtype, emp, total_n, rate))
        rows_out.sort(key=lambda r: -r[4])
        print(f"{'字段':<22}{'类型':<16}{'空值数':>10}{'/抽样':>10}{'空率':>10}")
        for fname, dtype, emp, tot, rate in rows_out:
            flag = "  <-- 需填补" if rate > 0 else ""
            print(f"{fname:<22}{dtype:<16}{emp:>10}{tot:>10}{rate:>9.1%}{flag}")


def main():
    cfg = load_config()
    print("################ ES 审计 ################")
    audit_es(cfg)
    print("\n################ Milvus 审计 ################")
    audit_milvus(cfg)


if __name__ == "__main__":
    main()
