#!/usr/bin/env python3
"""将电配花师 (dphs) 搭配 Excel 导入 Elasticsearch outfits 索引。

数据来源：``data/tables/dphs_outfits.xlsx``（sheet: outfits）
每条搭配写入 ES 时 ``source = "dphs_outfits"``。

用法（在 fila_agent_html 目录）::

  source .venv/bin/activate
  python3 scripts/build_dphs_outfits_es.py [--reset-source] [--dry-run]

  --reset-source  先删除 ES 中 source=dphs_outfits 的旧文档再写入
  --dry-run       仅解析并打印，不实际写入 ES
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
XLSX_PATH = ROOT / "data" / "tables" / "dphs_outfits.xlsx"

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
from backend.retrieval.outfit_color_series import outfit_color_series_tags
from scripts.etl_common import is_legacy_sku_id
from scripts.outfit_item_builder import (
    aggregate_outfit_season,
    build_outfit_item_from_sku,
    cartesian_split_items_by_role,
    load_skus_jsonl,
)

try:
    from elasticsearch import Elasticsearch
    from elasticsearch.helpers import bulk
except ImportError:
    Elasticsearch = None  # type: ignore
    bulk = None  # type: ignore

try:
    import openpyxl
except ImportError:
    openpyxl = None  # type: ignore

SOURCE = "dphs_outfits"


def dedupe_sku_ids(sku_ids: list[str]) -> list[str]:
    """搭配内 SKU 去重，保留首次出现顺序。"""
    return list(dict.fromkeys(sku_ids))


def connect_es(cfg: dict[str, Any]) -> Any:
    if Elasticsearch is None:
        logger.error("请安装: pip install 'elasticsearch>=7,<8'")
        raise SystemExit(1)
    es_cfg = cfg.get("elasticsearch") or {}
    hosts = get_elasticsearch_hosts(cfg)
    user = env_or_empty(str(es_cfg.get("username_env") or ""))
    pwd = env_or_empty(str(es_cfg.get("password_env") or ""))
    client = create_elasticsearch_client(
        hosts, username=user, password=pwd, timeout_sec=60,
    )
    if not client.ping():
        logger.error("无法连接 Elasticsearch: %s", hosts)
        raise SystemExit(1)
    logger.info("已连接 ES: %s", hosts)
    return client


def parse_xlsx(path: Path) -> list[dict[str, Any]]:
    """解析 dphs_outfits.xlsx → list of {outfit_id, sku_ids, tags, reason}。"""
    if openpyxl is None:
        logger.error("请安装: pip install openpyxl")
        raise SystemExit(1)
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb["outfits"]
    rows: list[dict[str, Any]] = []
    header: list[str] = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            header = [str(c or "").strip() for c in row]
            continue
        vals = {header[j]: (row[j] if j < len(row) else None) for j in range(len(header))}
        outfit_id = str(vals.get("搭配id") or "").strip()
        skus_raw = str(vals.get("skus") or "").strip()
        tags_raw = str(vals.get("tags") or "").strip()
        reason = str(vals.get("reason") or "").strip()
        if not outfit_id or not skus_raw:
            continue
        raw_sku_ids = [s.strip() for s in skus_raw.split(",") if s.strip()]
        # 硬性条件：仅保留新款货号（字母开头），过滤老款/电商款（数字开头）
        raw_sku_ids = [s for s in raw_sku_ids if not is_legacy_sku_id(s)]
        sku_ids = dedupe_sku_ids(raw_sku_ids)
        if len(raw_sku_ids) != len(sku_ids):
            logger.info(
                "搭配 %s 去除重复 SKU: %d -> %d",
                outfit_id,
                len(raw_sku_ids),
                len(sku_ids),
            )
        if len(sku_ids) < 2:
            logger.warning(
                "搭配 %s 去重后 SKU 不足 2 个，跳过",
                outfit_id,
            )
            continue
        tags = [t.strip().lstrip("#").strip() for t in tags_raw.split("#") if t.strip()]
        rows.append({
            "outfit_id": outfit_id,
            "sku_ids": sku_ids,
            "tags": tags,
            "reason": reason,
        })
    wb.close()
    logger.info("从 Excel 解析搭配 %d 条", len(rows))
    return rows


def fetch_sku_details(
    client: Any,
    sku_ids: list[str],
    index: str,
) -> dict[str, dict[str, Any]]:
    """[已废弃] 从 ES skus 索引 mget SKU 详情。保留签名以兼容外部调用，新流程改用 load_skus_jsonl。"""
    if not sku_ids:
        return {}
    try:
        res = client.mget(index=index, body={"ids": sku_ids})
        docs = res.get("docs") or []
        out: dict[str, dict[str, Any]] = {}
        for doc in docs:
            if not doc.get("found", False):
                continue
            src = doc.get("_source") or {}
            sid = str(src.get("sku_id") or "").strip()
            if sid:
                out[sid] = src
        return out
    except Exception as e:
        logger.warning("mget SKU 失败: %s", e)
        return {}


def _build_items(
    row: dict[str, Any],
    sku_details: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """从搭配行 + SKU 详情构建 item 列表（is_master 留待按组合重置）。"""
    sku_ids = dedupe_sku_ids(row["sku_ids"])
    items: list[dict[str, Any]] = []
    for sid in sku_ids:
        sku = sku_details.get(sid) or {}
        items.append(build_outfit_item_from_sku(sid, sku, False))
    return items


def _build_doc_from_items(
    outfit_id: str,
    items: list[dict[str, Any]],
    row: dict[str, Any],
) -> dict[str, Any]:
    """从一组 item（已笛卡尔拆分）构建单个 ES outfits 文档。"""
    for it in items:
        it["isMaster"] = False
        it["is_master"] = False
    if items:
        items[0]["isMaster"] = True
        items[0]["is_master"] = True

    sku_ids = [str(it.get("sku_id") or "").strip() for it in items]
    spu_ids: list[str] = []
    roles: list[str] = []
    price_total = 0.0
    genders: list[str] = []
    for it in items:
        spu_id = it.get("spu_id") or ""
        if spu_id:
            spu_ids.append(spu_id)
        role = it.get("role") or ""
        if role and role not in roles:
            roles.append(role)
        price_total += float(it.get("price") or 0.0)
        g = it.get("gender")
        if isinstance(g, list) and g:
            genders.append(str(g[0] or ""))
        elif g:
            genders.append(str(g))

    # 推断性别：取众数
    gender = ""
    if genders:
        from collections import Counter
        gender = Counter(genders).most_common(1)[0][0]

    tags = row.get("tags") or []
    reason = row.get("reason") or ""
    search_text = " ".join(tags) + " " + reason

    master_item = items[0] if items else {}
    color_tags = outfit_color_series_tags({"items": items})
    return {
        "outfit_id": outfit_id,
        "name": "",
        "shopName": "",
        "type": None,
        "flags": {},
        "leftHeroUrl": None,
        "backgroundImg": None,
        "search_text": search_text,
        "gender": gender,
        "source": SOURCE,
        "season": aggregate_outfit_season(items),
        "series_tags": [],
        "occasion_tags": [t for t in tags],
        "style_tags": [],
        "color_series_tags": color_tags,
        "roles": roles,
        "price_total": price_total,
        "status": 1,
        "display_image": master_item.get("display_image") or "",
        "index_image": master_item.get("display_image") or "",
        "background_img": "",
        "outfit_tryon_image": "",
        "master_sku_id": sku_ids[0] if sku_ids else "",
        "master_spu_id": master_item.get("spu_id") or "",
        "sku_ids": sku_ids,
        "spu_ids": list(dict.fromkeys(spu_ids)),
        "items": items,
        "dphs_reason": reason,
    }


def build_outfit_docs(
    row: dict[str, Any],
    sku_details: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """将一条搭配行 + SKU 详情构建为 ES outfits 文档列表。

    同一 role 存在多件 SKU 时按笛卡尔积拆成多套（每 role 取 1 件），
    从源头杜绝"一套搭配里同 role 多 sku"。每套 outfit_id 在 xlsx 搭配id
    基础上加组合序号后缀（idx 0 保持原 id，向后兼容）。不同 role 各 1 件
    时退化为单套。单 role 或拆分后 <2 件的行返回 []（无法成搭）。
    """
    base_id = row["outfit_id"]
    items = _build_items(row, sku_details)
    combos = cartesian_split_items_by_role(items)
    if not combos:
        return []
    docs: list[dict[str, Any]] = []
    for idx, combo in enumerate(combos):
        outfit_id = base_id if idx == 0 else f"{base_id}__c{idx}"
        docs.append(_build_doc_from_items(outfit_id, combo, row))
    return docs


def main() -> None:
    parser = argparse.ArgumentParser(
        description="导入电配花师搭配到 ES outfits 索引",
    )
    parser.add_argument(
        "--reset-source",
        action="store_true",
        help="先删除 ES 中 source=dphs_outfits 的旧文档再写入",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅解析并打印，不实际写入 ES",
    )
    parser.add_argument("--batch-size", type=int, default=200)
    args = parser.parse_args()

    if not XLSX_PATH.is_file():
        logger.error("找不到 Excel 文件: %s", XLSX_PATH)
        raise SystemExit(1)

    rows = parse_xlsx(XLSX_PATH)
    if not rows:
        logger.warning("未解析到搭配数据")
        return

    cfg = load_config()
    indices = get_elasticsearch_indices(cfg)
    ix_outfits = indices["outfits"]

    if args.dry_run:
        for r in rows[:3]:
            logger.info("DRY-RUN: %s → skus=%s", r["outfit_id"], r["sku_ids"])
        logger.info("DRY-RUN: 共 %d 条搭配，跳过 ES 写入", len(rows))
        return

    client = connect_es(cfg)

    # 收集所有需要查的 SKU ID
    all_sku_ids: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for sid in r["sku_ids"]:
            if sid not in seen:
                seen.add(sid)
                all_sku_ids.append(sid)
    logger.info("共需查询 %d 个不重复 SKU", len(all_sku_ids))

    # SKU 属性直接取自 build_catalog.py 产出的 skus.jsonl（单一事实源，与 micro_guide 一致）
    sku_map = load_skus_jsonl()
    logger.info("加载 skus.jsonl: %d 个 SKU", len(sku_map))
    sku_details: dict[str, dict[str, Any]] = {
        sid: sku_map[sid] for sid in all_sku_ids if sid in sku_map
    }
    logger.info("命中 %d / %d 个 SKU 详情", len(sku_details), len(all_sku_ids))

    missing = [sid for sid in all_sku_ids if sid not in sku_details]
    if missing:
        logger.warning("以下 SKU 在 skus.jsonl 中未找到: %s", missing[:20])

    # 删除旧数据
    if args.reset_source:
        try:
            res = client.delete_by_query(
                index=ix_outfits,
                body={"query": {"term": {"source": SOURCE}}},
                refresh=True,
                conflicts="proceed",
            )
            deleted = int(res.get("deleted") or 0)
            logger.info("已删除 source=%s 旧文档 %d 条", SOURCE, deleted)
        except Exception as e:
            logger.warning("删除旧文档失败: %s", e)

    # 过滤掉含有 ES 中找不到的 SKU 的搭配
    valid_rows: list[dict[str, Any]] = []
    skipped = 0
    for r in rows:
        missing_in_outfit = [sid for sid in r["sku_ids"] if sid not in sku_details]
        if missing_in_outfit:
            skipped += 1
            continue
        valid_rows.append(r)
    logger.info(
        "搭配过滤: 总计=%d, 有效=%d, 跳过=%d (含不存在的SKU)",
        len(rows), len(valid_rows), skipped,
    )

    # 构建 outfit 文档并批量写入
    actions: list[dict[str, Any]] = []
    split_rows = 0
    for r in valid_rows:
        docs = build_outfit_docs(r, sku_details)
        if not docs:
            continue
        if len(docs) > 1:
            split_rows += 1
        for doc in docs:
            actions.append({
                "_op_type": "index",
                "_index": ix_outfits,
                "_id": doc["outfit_id"],
                "_source": doc,
            })
    if split_rows:
        logger.info(
            "同 role 多 sku 搭配按笛卡尔积拆分: %d 行 → 多套", split_rows,
        )

    ok = err = 0
    for i in range(0, len(actions), args.batch_size):
        chunk = actions[i : i + args.batch_size]
        try:
            res = bulk(client, chunk, raise_on_error=False)
            if isinstance(res, tuple) and len(res) >= 2:
                ok += int(res[0])
                errs = res[1]
                if errs:
                    err += len(errs)
            else:
                ok += len(chunk)
        except Exception as e:
            logger.exception("bulk 写入失败: %s", e)
            err += len(chunk)

    client.indices.refresh(index=ix_outfits)
    logger.info(
        "搭配写入 ES 完成: index=%s, source=%s, 成功=%d, 失败=%d",
        ix_outfits, SOURCE, ok, err,
    )

    # 验证
    try:
        res = client.count(
            index=ix_outfits,
            body={"query": {"term": {"source": SOURCE}}},
        )
        count = int(res.get("count") or 0)
        logger.info("验证: ES 中 source=%s 搭配总数=%d", SOURCE, count)
    except Exception as e:
        logger.warning("验证查询失败: %s", e)


if __name__ == "__main__":
    main()
