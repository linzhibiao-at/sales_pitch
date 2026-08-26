#!/usr/bin/env python3
"""FILA SKU 文本向量索引（title → text_vector → Milvus）。

与 ``build_fila_milvus_lite_index.py``（主图 product_vector）分离：
  - 集合：config milvus.collections.sku_text_vectors（默认 fila_sku_text_vectors）
  - 向量字段：text_vector
  - 数据源：data/processed/skus.jsonl 的 title（经 sanitize，剔除货号/款号与噪声编码）

支持 local（Milvus Lite *.db）与 cloud（托管 Milvus），URI 由
``get_milvus_uri`` / ``FILA_MILVUS_URI`` / ``milvus.mode`` 决定。

用法（在 fila_agent_html 目录下）::

  source .venv/bin/activate
  export PYTHONPATH="$(pwd)"
  export ARK_API_KEY=...
  python3 scripts/build_text_milvus_index.py [--reset] [--incremental] [--batch-size 128]

更新策略与 ``build_fila_milvus_lite_index.py`` 一致：
  - **全量**：默认对所有行 upsert；``--reset`` 删除集合并重建 schema。
  - **增量**：``--incremental`` 仅当 title + 维度 + 模型名变化时重算。
  - **孤立向量**：``--prune-orphans`` 删除 JSONL 中已不存在的主键。

状态文件：``data/logs/fila_index_sync_state.json`` 的 ``milvus.sku_text_vectors``。
"""

from __future__ import annotations

import argparse
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
logger = logging.getLogger("build_text_milvus_index")

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from index_sync_state import (
    DEFAULT_STATE_PATH,
    clear_milvus_bucket,
    load_state,
    milvus_text_row_signature,
    save_state,
)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.empty_image_urls import sku_has_empty_tryon_image
from backend.intent.color_series_mapper import map_color_to_series_list
from backend.intent.sku_attributes import enrich_sku_attributes
from scripts.etl_common import up_time_to_epoch

DataType = None  # type: ignore
MilvusClient = None  # type: ignore
Function = None  # type: ignore
FunctionType = None  # type: ignore
_IMPORT_ERR: Exception | None = None


def _import_pymilvus() -> None:
    global DataType, MilvusClient, Function, FunctionType, _IMPORT_ERR
    if MilvusClient is not None:
        return
    try:
        from pymilvus import (
            DataType as _DT,
            Function as _Function,
            FunctionType as _FunctionType,
            MilvusClient as _MC,
        )
    except ImportError as exc:
        _IMPORT_ERR = exc
        return
    DataType = _DT
    Function = _Function
    FunctionType = _FunctionType
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


def create_sku_text_collection(
    client: Any,
    name: str,
    dim: int,
    uri: str,
    mv: dict[str, Any],
    vf: str,
) -> None:
    from backend.config import is_milvus_lite_local_uri

    _import_pymilvus()
    # Lite 不支持 BM25 function；cloud 走 hybrid（search_text → sparse_vector）。
    with_bm25 = not is_milvus_lite_local_uri(uri)
    schema = client.create_schema()
    schema.add_field(
        "sku_id",
        DataType.VARCHAR,
        max_length=64,
        is_primary=True,
        auto_id=False,
    )
    schema.add_field(vf, DataType.FLOAT_VECTOR, dim=dim)
    schema.add_field("spu_id", DataType.VARCHAR, max_length=32)
    schema.add_field("product_name", DataType.VARCHAR, max_length=256)
    schema.add_field("product_intro", DataType.VARCHAR, max_length=2048)
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
    if with_bm25:
        # search_text 喂 BM25 Function 自动生成 sparse_vector（稀疏关键词召回）
        th = mv.get("text_hybrid") or {}
        search_text_field = str(th.get("search_text_field") or "search_text")
        sparse_field = str(th.get("sparse_vector_field") or "sparse_vector")
        analyzer_params = th.get("analyzer_params") or {"type": "chinese"}
        schema.add_field(
            search_text_field,
            DataType.VARCHAR,
            max_length=8192,
            enable_analyzer=True,
            analyzer_params=analyzer_params,
        )
        schema.add_field(sparse_field, DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(
            Function(
                name="search_text_bm25",
                input_field_names=[search_text_field],
                output_field_names=[sparse_field],
                function_type=FunctionType.BM25,
            )
        )
    client.create_collection(name, schema=schema)
    idx = client.prepare_index_params()
    index_type, extra = _vector_index_type_and_params(uri, mv)
    metric = extra.get("metric_type", "COSINE")
    params = extra.get("params") or {}
    idx.add_index(
        vf,
        index_type=index_type,
        index_name="sku_text_vec_idx",
        metric_type=metric,
        params=params,
    )
    if with_bm25:
        th = mv.get("text_hybrid") or {}
        sparse_field = str(th.get("sparse_vector_field") or "sparse_vector")
        sparse_idx = th.get("sparse_index") or {}
        idx.add_index(
            sparse_field,
            index_type=str(sparse_idx.get("index_type") or "SPARSE_INVERTED_INDEX"),
            index_name="sku_text_sparse_idx",
            metric_type=str(sparse_idx.get("metric_type") or "BM25"),
            params=sparse_idx.get("params") or {"drop_ratio_build": 0.2},
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


def _chunks(ids: List[str], size: int) -> Iterator[List[str]]:
    for i in range(0, len(ids), size):
        yield ids[i : i + size]


# color_name/color_series 噪声标记：含这些的色名不嵌入向量（如 "颜色:;尺码:"）
_COLOR_NOISE_MARKERS = ("颜色", "尺码", ":", ";", "[", "{", "table", "SIZE")


def _clean_color_text(color_name: str, color_series: str) -> str:
    """返回可嵌入的色名+色系文本，过滤噪声色名。"""
    parts: list[str] = []
    cn = str(color_name or "").strip()
    if cn and not any(m in cn for m in _COLOR_NOISE_MARKERS):
        parts.append(cn)
    cs = str(color_series or "").strip()
    if cs and not any(m in cs for m in _COLOR_NOISE_MARKERS):
        parts.append(cs)
    return " ".join(parts)


def _build_intro_src(row: dict[str, Any]) -> str:
    """构建嵌入文本：title + 色名/色系 + 季节。

    text-vector 索引原本只 embed title，title 不含颜色/季节，导致 keyword 里的
    color/season 无法 boost sim。把 color_name/color_series/season 拼进嵌入文本，
    使向量捕获这些维度，keyword 即可按 color/season 语义匹配。
    """
    parts = [str(row.get("title") or "").strip()]
    color_text = _clean_color_text(row.get("color_name"), row.get("color_series"))
    if color_text:
        parts.append(color_text)
    season = row.get("season")
    if isinstance(season, list):
        season_str = " ".join(str(s).strip() for s in season if str(s).strip())
    else:
        season_str = str(season or "").strip()
    if season_str:
        parts.append(season_str)
    return " ".join(p for p in parts if p)


def _build_search_text(intro: str, row: dict[str, Any], season_s: str) -> str:
    """BM25 语料：嵌入文本 + 结构化属性，供 sparse 关键词召回匹配。

    dense ``text_vector`` 嵌入的是 sanitize 后的 intro（语义）；
    search_text 在此基础上补 category/role/series/group_brand/modeling/season，
    使 BM25 能按这些枚举词精确命中。
    """
    tokens = [str(intro or "").strip()]
    for key in ("category_l2", "role", "series", "group_brand", "modeling"):
        v = row.get(key)
        if isinstance(v, list):
            v = " ".join(str(x) for x in v if x)
        if v:
            tokens.append(str(v))
    if season_s:
        # season_s 形如 "春,夏"；展开成空格分隔便于分词
        tokens.append(str(season_s).replace(",", " "))
    return " ".join(t for t in tokens if t)


def verify_search(
    client: Any,
    collection: str,
    vector_field: str,
    sample_vector: list[float],
) -> bool:
    logger.info("Verify search on %s ...", collection)
    try:
        res = client.search(
            collection_name=collection,
            data=[sample_vector],
            anns_field=vector_field,
            limit=3,
            output_fields=["sku_id"],
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
                "  #%d sku_id=%s dist=%s",
                i,
                ent.get("sku_id"),
                dist,
            )
        return True
    except Exception as exc:
        logger.exception("Verify failed: %s", exc)
        return False


def index_sku_text_vectors(
    client: Any,
    collection: str,
    vector_field: str,
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
    prior_ids: set[str],
    log_path: Path,
    with_bm25: bool = False,
) -> tuple[int, int, list[float] | None]:
    from backend.text_sanitize import sanitize_text_for_embedding

    cfg = load_yaml_config()
    proc = ROOT / (cfg.get("paths") or {}).get("processed_dir", "data/processed")
    path = proc / "skus.jsonl"
    if not path.is_file():
        logger.warning("Missing %s", path)
        return 0, 0, None

    sku_rows: dict[str, dict[str, Any]] = {}
    file_sigs: dict[str, str] = {}
    filtered_empty_tryon = 0
    for row in iter_jsonl(path):
        sku_id = str(row.get("sku_id") or "").strip()
        if not sku_id:
            continue
        if sku_has_empty_tryon_image(row):
            filtered_empty_tryon += 1
            continue
        # 兜底推导结构化属性（jsonl 理论上已有，缺失则按 title+category_l2 实时推导）
        enrich_sku_attributes(row)
        intro_src = _build_intro_src(row)
        sku_rows[sku_id] = row
        file_sigs[sku_id] = milvus_text_row_signature(
            intro_src,
            dimensions=dim,
            embedding_model=embedding_model,
        )
    if filtered_empty_tryon:
        logger.info(
            "SKU 跳过占位 tryon_image: %d 条",
            filtered_empty_tryon,
        )

    target_ids = [
        sid
        for sid, sig in file_sigs.items()
        if (not incremental)
        or state["milvus"]["sku_text_vectors"].get(sid) != sig
    ]
    if test_limit > 0:
        target_ids = target_ids[:test_limit]
        logger.info("TEST mode: at most %d SKU text upserts", len(target_ids))

    batch: list[dict[str, Any]] = []
    ok = skip = 0
    bad_emb_detail = 0
    bad_emb_cap = 5
    first_vec: list[float] | None = None
    total = len(target_ids)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with log_path.open("w", encoding="utf-8") as logf:
        for idx, sku_id in enumerate(target_ids, 1):
            row = sku_rows.get(sku_id)
            if row is None:
                skip += 1
                continue
            spu = str(row.get("spu_id") or "")[:32]
            intro_src = _build_intro_src(row)
            intro = sanitize_text_for_embedding(
                intro_src,
                sku_ids=[sku_id],
                spu_ids=[spu] if spu else None,
            )
            if len(intro) < 4:
                skip += 1
                logf.write(
                    json.dumps(
                        {
                            "sku_id": sku_id,
                            "ok": False,
                            "reason": "empty_title",
                        },
                        ensure_ascii=False,
                    )
                    + "\n",
                )
                continue
            vec = embed_fn(intro)
            if not vec or len(vec) != dim:
                skip += 1
                if bad_emb_detail < bad_emb_cap:
                    logger.warning(
                        "[%d/%d] bad text embedding sku_id=%s len=%s",
                        idx,
                        total or 1,
                        sku_id,
                        len(vec) if vec else None,
                    )
                    bad_emb_detail += 1
                logf.write(
                    json.dumps(
                        {
                            "sku_id": sku_id,
                            "ok": False,
                            "reason": "embed_failed",
                        },
                        ensure_ascii=False,
                    )
                    + "\n",
                )
                continue
            if first_vec is None:
                first_vec = vec
            season = row.get("season") or []
            if isinstance(season, list):
                season_s = ",".join(str(x) for x in season if x)[:256]
            else:
                season_s = str(season)[:256]
            row_out: dict[str, Any] = {
                    "sku_id": sku_id[:64],
                    vector_field: vec,
                    "spu_id": spu[:32],
                    "product_name": str(row.get("title") or "")[:256],
                    "product_intro": intro[:2048],
                    "color_series": [s[:32] for s in map_color_to_series_list(
                        str(row.get("attr_name") or row.get("color_name") or ""),
                    )][:8],
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
            }
            if with_bm25:
                # search_text 喂 BM25；sparse_vector 由 Function 自动生成，禁止手动写入
                row_out["search_text"] = _build_search_text(intro, row, season_s)[:8192]
            batch.append(row_out)
            logf.write(
                json.dumps(
                    {"sku_id": sku_id, "ok": True, "dim": len(vec)},
                    ensure_ascii=False,
                )
                + "\n",
            )
            if len(batch) >= batch_size:
                client.upsert(collection, batch)
                ok += len(batch)
                batch.clear()
                logger.info(
                    "[%d/%d] text upserted=%d skip=%d",
                    idx,
                    total,
                    ok,
                    skip,
                )
                time.sleep(0.02)
        if batch:
            client.upsert(collection, batch)
            ok += len(batch)
    client.flush(collection)

    if not skip_state:
        state["milvus"]["sku_text_vectors"] = file_sigs
        if prune_orphans and prior_ids:
            dead = list(prior_ids - set(file_sigs.keys()))
            for part in _chunks(dead, 200):
                client.delete(collection, ids=part)
            logger.info("Milvus 文本向量删除孤立主键: %d", len(dead))

    if skip and ok == 0 and total:
        logger.error(
            "文本向量全部失败：请检查 ARK_API_KEY、embedding 配置与网络。",
        )
    logger.info("SKU text done: upserted=%d skipped=%d", ok, skip)
    return ok, skip, first_vec


def main() -> None:
    parser = argparse.ArgumentParser(
        description="FILA SKU 文本向量化并写入 Milvus sku_text_vectors",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="删除现有文本向量集合并重建",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="仅对 title 签名变化的记录重算向量并 upsert",
    )
    parser.add_argument(
        "--prune-orphans",
        action="store_true",
        help="删除集合中已不在 skus.jsonl 的主键",
    )
    parser.add_argument(
        "--state-file",
        type=str,
        default="",
        help=f"状态 JSON 路径（默认 {DEFAULT_STATE_PATH}）",
    )
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="仅处理前 N 条（smoke test）",
    )
    parser.add_argument(
        "--collection",
        type=str,
        default="",
        help="覆盖目标集合名（默认 config milvus.collections.sku_text_vectors）；用于 smoke test",
    )
    args = parser.parse_args()

    cfg = load_yaml_config()
    mv_cfg = cfg.get("milvus") or {}
    if not mv_cfg.get("enabled"):
        raise SystemExit("milvus.enabled 为 false")

    from backend.config import (
        get_milvus_token,
        get_milvus_uri,
        restore_stashed_milvus_uri,
        stash_milvus_db_uri_before_pymilvus_import,
    )
    from backend.embedding_client import embed_text

    uri_env = str(mv_cfg.get("uri_env") or "FILA_MILVUS_URI")
    stash_milvus_db_uri_before_pymilvus_import(uri_env)
    try:
        _import_pymilvus()
    finally:
        restore_stashed_milvus_uri()

    if _IMPORT_ERR is not None or MilvusClient is None:
        logger.error("需要 pymilvus、milvus，见 requirements.txt")
        raise SystemExit(1) from _IMPORT_ERR

    uri = get_milvus_uri(cfg)
    token = get_milvus_token(cfg)
    if not uri:
        raise SystemExit(
            "无法解析 Milvus URI：请设置 FILA_MILVUS_URI，"
            "或 config milvus.mode=cloud + cloud.uri，"
            "或 milvus.local_data_file",
        )

    from backend.config import is_milvus_lite_local_uri

    with_bm25 = not is_milvus_lite_local_uri(uri)
    if not with_bm25:
        logger.warning("本地 *.db（Lite）不支持 BM25 function，降级 dense-only")

    state_path = (
        Path(args.state_file).expanduser().resolve()
        if args.state_file.strip()
        else DEFAULT_STATE_PATH
    )
    state = load_state(state_path)
    prior_ids = set(state["milvus"]["sku_text_vectors"])

    emb_cfg = cfg.get("embedding") or {}
    dim = int(emb_cfg.get("dimensions") or 1024)
    embedding_model = str(emb_cfg.get("model") or "")
    vf = str(mv_cfg.get("text_vector_field") or "text_vector")
    col_name = str(args.collection).strip() or str(
        (mv_cfg.get("collections") or {}).get("sku_text_vectors")
        or "fila_sku_text_vectors",
    )

    logger.info("Milvus URI: %s", uri)
    client = MilvusClient(uri=uri, token=token or None)

    if args.reset:
        clear_milvus_bucket(state, "sku_text_vectors")
        if client.has_collection(col_name):
            client.drop_collection(col_name)
            logger.info("Dropped collection: %s", col_name)

    if not client.has_collection(col_name):
        create_sku_text_collection(client, col_name, dim, uri, mv_cfg, vf)

    test_n = args.limit if args.limit > 0 else 0
    skip_state = test_n > 0
    prune_ids = (
        prior_ids
        if (args.prune_orphans and not args.reset)
        else set()
    )
    log_path = ROOT / "data/logs/etl/embedding_milvus_text.jsonl"

    ok_n, skip_n, first_vec = index_sku_text_vectors(
        client,
        col_name,
        vf,
        embed_text,
        dim,
        args.batch_size,
        test_n,
        embedding_model=embedding_model,
        incremental=args.incremental,
        prune_orphans=args.prune_orphans,
        state=state,
        skip_state=skip_state,
        prior_ids=prune_ids,
        log_path=log_path,
        with_bm25=with_bm25,
    )

    if not skip_state:
        save_state(state, state_path)

    logger.info("log: %s", log_path)
    logger.info("sku text vectors ok=%s skip=%s", ok_n, skip_n)

    if test_n > 0:
        if first_vec is None:
            logger.error("TEST: 无文本向量写入，请检查 ARK_API_KEY 与网络")
            raise SystemExit(1)
        if not verify_search(client, col_name, vf, first_vec):
            raise SystemExit(1)
        logger.info("Smoke test OK")

    print("\n文本向量索引构建结束。运行时 Milvus URI:\n")
    print(f"  {uri}")
    if not skip_state:
        print(f"\n状态文件: {state_path}")


if __name__ == "__main__":
    main()
