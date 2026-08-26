#!/usr/bin/env python3
"""将 outfits_unique.txt 搭配数据导入 Elasticsearch outfits 索引。

数据来源：``data/tables/outfits_unique.txt``
每行格式为 ``角色SKU-角色SKU-...``（角色为中文，SKU 为货号）。
每条搭配写入 ES 时 ``source = "outfits_unique"``。

用法（在 fila_agent_html 目录）::

  source .venv/bin/activate
  python3 scripts/build_outfits_unique_es.py [--reset-source] [--dry-run]

  --reset-source  先删除 ES 中 source=outfits_unique 的旧文档再写入
  --dry-run       仅解析并打印，不实际写入 ES
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
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
TXT_PATH = ROOT / "data" / "tables" / "outfits_unique.txt"

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

SOURCE = "outfits_unique"


def dedupe_sku_ids(sku_ids: list[str]) -> list[str]:
    """搭配内 SKU 去重，保留首次出现顺序。"""
    return list(dict.fromkeys(sku_ids))
_SKU_TAIL_RE = re.compile(
    r"([A-Z]\d{2}[UMWV]\d+[A-Z0-9]+)$",
    re.IGNORECASE,
)


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


def _parse_segment(segment: str) -> tuple[str, str]:
    """从 ``角色SKU`` 或纯 SKU 片段解析 (role, sku_id)。"""
    seg = segment.strip()
    if not seg:
        return "", ""

    if re.fullmatch(r"[A-Z0-9]+", seg, re.IGNORECASE):
        sku_id = seg.upper()
        return "", sku_id

    role_match = re.match(r"^([\u4e00-\u9fff]+)(.+)$", seg)
    if role_match:
        role = role_match.group(1).strip()
        tail = role_match.group(2).strip()
        sku_match = _SKU_TAIL_RE.search(tail)
        if sku_match:
            return role, sku_match.group(1).upper()
        if re.fullmatch(r"[A-Z0-9]+", tail, re.IGNORECASE):
            return role, tail.upper()

    sku_match = _SKU_TAIL_RE.search(seg)
    if sku_match:
        sku_id = sku_match.group(1).upper()
        role = seg[: sku_match.start()].strip()
        return role, sku_id

    return "", ""


def _make_outfit_id(sku_ids: list[str], raw_line: str) -> str:
    """基于 SKU 列表生成稳定的 outfit_id。"""
    key = "-".join(sku_ids) if sku_ids else raw_line
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:12]
    return f"outfits_unique_{digest}"


def parse_txt(path: Path) -> list[dict[str, Any]]:
    """解析 outfits_unique.txt → list of outfit rows。"""
    rows: list[dict[str, Any]] = []
    skipped = 0
    text = path.read_text(encoding="utf-8")
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        sku_ids: list[str] = []
        roles: list[str] = []
        for segment in line.split("-"):
            role, sku_id = _parse_segment(segment)
            if not sku_id:
                skipped += 1
                logger.warning(
                    "第 %d 行片段无法解析 SKU，跳过整行: %s",
                    line_no,
                    segment,
                )
                sku_ids = []
                break
            # 硬性条件：仅保留新款货号（字母开头），过滤老款/电商款（数字开头）
            if is_legacy_sku_id(sku_id):
                continue
            if sku_id in sku_ids:
                continue
            sku_ids.append(sku_id)
            roles.append(role)

        if not sku_ids:
            continue
        if len(sku_ids) < 2:
            logger.warning(
                "第 %d 行去重后 SKU 不足 2 个，跳过: %s",
                line_no,
                line,
            )
            continue

        rows.append({
            "outfit_id": _make_outfit_id(sku_ids, line),
            "sku_ids": sku_ids,
            "roles": roles,
            "tags": [],
            "reason": "",
            "raw_line": line,
        })

    logger.info(
        "从文本解析搭配 %d 条（跳过 %d 行）",
        len(rows),
        skipped,
    )
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
    """从搭配行 + SKU 详情构建 item 列表（is_master 留待按组合重置）。

    role 优先取 skus.jsonl 归一化值（build_outfit_item_from_sku 投影），
    缺失时回退到 txt 解析的角色（row['roles'] 与 sku_ids 同序）——鞋类等
    upDown 为空的 SKU role 可能为空，回退保证能按 role 分组做笛卡尔拆分。
    """
    sku_ids = dedupe_sku_ids(row["sku_ids"])
    parsed_roles: list[str] = list(row.get("roles") or [])
    items: list[dict[str, Any]] = []
    for idx, sid in enumerate(sku_ids):
        sku = sku_details.get(sid) or {}
        item = build_outfit_item_from_sku(sid, sku, False)
        if not (item.get("role") or "").strip():
            fallback = parsed_roles[idx] if idx < len(parsed_roles) else ""
            if fallback:
                item["role"] = fallback
        items.append(item)
    return items


def _build_doc_from_items(
    outfit_id: str,
    items: list[dict[str, Any]],
    row: dict[str, Any],
) -> dict[str, Any]:
    """从一组 item（已笛卡尔拆分）构建单个 ES outfits 文档。"""
    # 每组合内重置 master 标记：仅首件为 master（保持 txt 首位角色为主推）
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

    gender = ""
    if genders:
        from collections import Counter
        gender = Counter(genders).most_common(1)[0][0]

    tags = row.get("tags") or []
    reason = row.get("reason") or ""
    search_text = " ".join(tags)
    if reason:
        search_text = f"{search_text} {reason}".strip()

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
        "occasion_tags": [],
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
        "outfits_unique_raw": row.get("raw_line") or "",
    }


def build_outfit_docs(
    row: dict[str, Any],
    sku_details: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """将一条搭配行 + SKU 详情构建为 ES outfits 文档列表。

    同一 role 存在多件 SKU 时按笛卡尔积拆成多套（每 role 取 1 件），
    从源头杜绝"一套搭配里同 role 多 sku"。每套 outfit_id 由其 SKU 组合
    稳定哈希生成（不同组合 → 不同 id）。不同 role 各 1 件时退化为单套，
    outfit_id 与旧逻辑一致。单 role 或拆分后 <2 件的行返回 []（无法成搭）。
    """
    items = _build_items(row, sku_details)
    combos = cartesian_split_items_by_role(items)
    if not combos:
        return []
    docs: list[dict[str, Any]] = []
    for combo in combos:
        sku_ids = [str(it.get("sku_id") or "").strip() for it in combo]
        outfit_id = _make_outfit_id(sku_ids, row.get("raw_line") or "")
        docs.append(_build_doc_from_items(outfit_id, combo, row))
    return docs


def main() -> None:
    parser = argparse.ArgumentParser(
        description="导入 outfits_unique.txt 搭配到 ES outfits 索引",
    )
    parser.add_argument(
        "--reset-source",
        action="store_true",
        help="先删除 ES 中 source=outfits_unique 的旧文档再写入",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅解析并打印，不实际写入 ES",
    )
    parser.add_argument("--batch-size", type=int, default=200)
    args = parser.parse_args()

    if not TXT_PATH.is_file():
        logger.error("找不到文本文件: %s", TXT_PATH)
        raise SystemExit(1)

    rows = parse_txt(TXT_PATH)
    if not rows:
        logger.warning("未解析到搭配数据")
        return

    if args.dry_run:
        for r in rows[:3]:
            logger.info(
                "DRY-RUN: %s → skus=%s raw=%s",
                r["outfit_id"],
                r["sku_ids"],
                r.get("raw_line"),
            )
        logger.info("DRY-RUN: 共 %d 条搭配，跳过 ES 写入", len(rows))
        return

    cfg = load_config()
    indices = get_elasticsearch_indices(cfg)
    ix_outfits = indices["outfits"]

    client = connect_es(cfg)

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
    logger.info(
        "命中 %d / %d 个 SKU 详情",
        len(sku_details),
        len(all_sku_ids),
    )

    missing = [sid for sid in all_sku_ids if sid not in sku_details]
    if missing:
        logger.warning("以下 SKU 在 skus.jsonl 中未找到: %s", missing[:20])

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

    valid_rows: list[dict[str, Any]] = []
    skipped = 0
    for r in rows:
        missing_in_outfit = [
            sid for sid in r["sku_ids"] if sid not in sku_details
        ]
        if missing_in_outfit:
            skipped += 1
            continue
        valid_rows.append(r)
    logger.info(
        "搭配过滤: 总计=%d, 有效=%d, 跳过=%d (含不存在的SKU)",
        len(rows),
        len(valid_rows),
        skipped,
    )

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
        ix_outfits,
        SOURCE,
        ok,
        err,
    )

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
