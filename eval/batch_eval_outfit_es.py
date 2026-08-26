"""ES utilities for batch-eval outfit documents.

Usage:
    cd fila_agent_html
    python -m eval.batch_eval_outfit_es delete --dry-run
    python -m eval.batch_eval_outfit_es delete --yes
    python -m eval.batch_eval_outfit_es index-results eval/results/top__polo.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import get_root
from backend.retrieval.es_client import EsClient

logger = logging.getLogger("batch_eval_outfit_es")

BATCH_EVAL_SOURCE_PREFIX = "batch_eval"


def batch_eval_source(recall_source: Any) -> str:
    suffix = str(recall_source or "unknown").strip() or "unknown"
    return f"{BATCH_EVAL_SOURCE_PREFIX}_{suffix}"


def batch_eval_outfit_id(
    input_sku_id: str,
    original_outfit_id: str,
    rank_order: int,
) -> str:
    oid = str(original_outfit_id or "").strip()
    raw = f"{input_sku_id}__{rank_order:02d}__{oid}"
    short_hash = hashlib.md5(raw.encode()).hexdigest()[:8]
    return f"{BATCH_EVAL_SOURCE_PREFIX}_{short_hash}"


def _outfit_item_ids(outfit: dict[str, Any]) -> tuple[list[str], list[str]]:
    sku_ids: list[str] = []
    spu_ids: list[str] = []
    for item in outfit.get("items") or []:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("sku_id") or "").strip()
        pid = str(item.get("spu_id") or "").strip()
        if sid:
            sku_ids.append(sid)
        if pid:
            spu_ids.append(pid)
    return list(dict.fromkeys(sku_ids)), list(dict.fromkeys(spu_ids))



def eval_outfit_doc(
    outfit: dict[str, Any],
    *,
    input_sku_id: str,
    input_sku: dict[str, Any],
    rank_order: int,
) -> tuple[str, dict[str, Any]] | None:
    original_oid = str(outfit.get("outfit_id") or "").strip()
    if not original_oid:
        return None
    recall_source = outfit.get("recall_source") or outfit.get("source") or ""
    doc_id = batch_eval_outfit_id(input_sku_id, original_oid, rank_order)
    sku_ids, spu_ids = _outfit_item_ids(outfit)
    item_titles = [
        str(item.get("title") or "")
        for item in outfit.get("items") or []
        if isinstance(item, dict)
    ]
    doc = dict(outfit)
    doc.update({
        "outfit_id": doc_id,
        "original_outfit_id": original_oid,
        "source": batch_eval_source(recall_source),
        "recall_source": recall_source,
        "batch_eval_input_sku_id": input_sku_id,
        "batch_eval_input_sku": input_sku,
        "batch_eval_rank_order": rank_order,
        "batch_eval_created_at": datetime.now(timezone.utc).isoformat(),
        "sku_ids": sku_ids,
        "spu_ids": spu_ids,
        "roles": list(dict.fromkeys(
            str(item.get("role") or "").strip()
            for item in outfit.get("items") or []
            if isinstance(item, dict) and str(item.get("role") or "").strip()
        )),
        "search_text": " ".join(
            x for x in [
                str(outfit.get("name") or ""),
                str(outfit.get("reason") or ""),
                str(input_sku.get("title") or ""),
                str(input_sku.get("gender") or ""),
                str(input_sku.get("category_l2") or ""),
                *item_titles,
            ]
            if x
        ),
    })
    return doc_id, doc


def build_batch_eval_docs(
    outfits: list[dict[str, Any]],
    *,
    input_sku_id: str,
    input_sku: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    docs: list[tuple[str, dict[str, Any]]] = []
    for rank_order, outfit in enumerate(outfits, start=1):
        if not isinstance(outfit, dict):
            continue
        converted = eval_outfit_doc(
            outfit,
            input_sku_id=input_sku_id,
            input_sku=input_sku,
            rank_order=rank_order,
        )
        if converted:
            docs.append(converted)
    return docs


def index_batch_eval_outfits(
    es: EsClient,
    outfits: list[dict[str, Any]],
    *,
    input_sku_id: str,
    input_sku: dict[str, Any],
) -> tuple[list[str], int, int]:
    docs = build_batch_eval_docs(
        outfits,
        input_sku_id=input_sku_id,
        input_sku=input_sku,
    )
    if not docs:
        return [], 0, 0
    if not es.available:
        raise RuntimeError("ES 不可用，无法写入 batch_eval outfits")
    ok, err = es.bulk_upsert_docs("outfits", docs)
    if err:
        logger.warning(
            "batch_eval outfits 写入 ES 部分失败: %d/%d (input_sku=%s)",
            err, len(docs), input_sku_id,
        )
    return [doc_id for doc_id, _ in docs], ok, err


def batch_eval_delete_query(
    *,
    sources: Iterable[str] | None = None,
    input_sku_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    filters: list[dict[str, Any]] = []
    source_terms = [str(x).strip() for x in (sources or []) if str(x).strip()]
    if source_terms:
        filters.append({"terms": {"source": source_terms}})
    else:
        filters.append({"prefix": {"source": f"{BATCH_EVAL_SOURCE_PREFIX}_"}})

    sku_terms = [str(x).strip() for x in (input_sku_ids or []) if str(x).strip()]
    if sku_terms:
        filters.append({"terms": {"batch_eval_input_sku_id": sku_terms}})

    return {"bool": {"filter": filters}}


def delete_batch_eval_outfits(
    es: EsClient,
    *,
    sources: Iterable[str] | None = None,
    input_sku_ids: Iterable[str] | None = None,
    dry_run: bool = False,
) -> int:
    if not es.available:
        raise RuntimeError("ES 不可用，无法删除 batch_eval outfits")
    query = batch_eval_delete_query(sources=sources, input_sku_ids=input_sku_ids)
    if dry_run:
        return es.count_docs("outfits", query)
    return es.delete_docs_by_query("outfits", query)


def _load_result_entries(paths: list[str]) -> list[dict[str, Any]]:
    root = get_root()
    entries: list[dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_absolute():
            path = root / path
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            entries.extend(x for x in data if isinstance(x, dict))
        elif isinstance(data, dict) and isinstance(data.get("results"), list):
            entries.extend(x for x in data["results"] if isinstance(x, dict))
        else:
            raise ValueError(f"不支持的结果文件格式: {path}")
    return entries


def _entry_outfits(entry: dict[str, Any]) -> list[dict[str, Any]]:
    outfits = entry.get("outfits")
    if isinstance(outfits, list):
        return [x for x in outfits if isinstance(x, dict)]
    snapshots: list[dict[str, Any]] = []
    for meta in entry.get("outfit_meta") or []:
        if isinstance(meta, dict) and isinstance(meta.get("snapshot"), dict):
            snapshots.append(meta["snapshot"])
    return snapshots


def index_result_files(es: EsClient, paths: list[str]) -> tuple[int, int]:
    ok_total = 0
    err_total = 0
    for entry in _load_result_entries(paths):
        input_sku_id = str(entry.get("input_sku_id") or "").strip()
        input_sku = entry.get("input_sku") or {}
        outfits = _entry_outfits(entry)
        if not input_sku_id or not outfits:
            continue
        _ids, ok, err = index_batch_eval_outfits(
            es,
            outfits,
            input_sku_id=input_sku_id,
            input_sku=input_sku if isinstance(input_sku, dict) else {},
        )
        ok_total += ok
        err_total += err
    return ok_total, err_total


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="批量评测搭配 ES 写入/删除工具")
    sub = parser.add_subparsers(dest="command", required=True)

    index_parser = sub.add_parser("index-results", help="从评测结果文件写入 batch_eval outfits")
    index_parser.add_argument("paths", nargs="+", help="评测结果 JSON 文件，路径相对 fila_agent_html")

    delete_parser = sub.add_parser("delete", help="删除 ES 中的 batch_eval outfits")
    delete_parser.add_argument("--source", action="append", default=[], help="只删除指定 source，可重复")
    delete_parser.add_argument("--input-sku-id", action="append", default=[], help="只删除指定输入 SKU，可重复")
    delete_parser.add_argument("--dry-run", action="store_true", help="只统计将删除的数量")
    delete_parser.add_argument("--yes", action="store_true", help="确认执行删除")

    args = parser.parse_args()
    es = EsClient()

    if args.command == "index-results":
        ok, err = index_result_files(es, args.paths)
        logger.info("写入完成: success=%d error=%d", ok, err)
        return

    if args.command == "delete":
        if not args.dry_run and not args.yes:
            raise SystemExit("删除需要显式传 --yes；可先用 --dry-run 预估数量")
        n = delete_batch_eval_outfits(
            es,
            sources=args.source,
            input_sku_ids=args.input_sku_id,
            dry_run=args.dry_run,
        )
        action = "将删除" if args.dry_run else "已删除"
        logger.info("%s batch_eval outfits: %d", action, n)


if __name__ == "__main__":
    main()
