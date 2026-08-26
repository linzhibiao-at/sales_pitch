#!/usr/bin/env python3
"""共享：从 skus.jsonl 记录构建 outfit item（dphs / outfits_unique 通用）。

dphs、outfits_unique 两个搭配源的 outfit item 之前各自从 ES skus 索引 mget 取
SKU 属性、且 item 的 `color` 块恒为 `{}`（ES sku 文档无 color 字段）、`attributes`
为空 `{}`（缺 sex，gender 过滤靠 fallback）。本模块统一改为直接读
``build_catalog.py`` 产出的 ``data/processed/skus.jsonl``（单一事实源），并补齐
`color` 块与 `attributes`，使 dphs/unique item 与 micro_guide item、ES skus 索引
的 SKU 属性完全一致。

skus.jsonl 不携带多图集合（outfitCd/outfitCps）与 swatch——这些字段置空
（dphs/unique 无原始 product_image 表）；`images.cover` 用 select_images.py
精选的 tryon/display 图。
"""

from __future__ import annotations

import json
import math
from itertools import product as _iter_product
from pathlib import Path
from typing import Any, Callable

from scripts.etl_common import processed_dir

SEASON_ORDER = ("春", "夏", "秋", "冬")


def aggregate_outfit_season(items: list[dict]) -> list[str]:
    """按 item.season 多数投票选 outfit 季节标签，并列全取，按春夏秋冬排序。

    遍历搭配内每个 item 的 ``season`` 列表（已由 ``build_outfit_item_from_sku``
    从 skus.jsonl 投影为 ``[春/夏/秋/冬]`` 子集），对四季逐季计数；取计数最大者。
    并列时全部保留（如 2 春 2 夏 → ``[春, 夏]``），无任何季节属性返回 ``[]``。
    """
    counts = {s: 0 for s in SEASON_ORDER}
    for item in items or []:
        seasons = item.get("season") if isinstance(item, dict) else None
        if not isinstance(seasons, list):
            continue
        for s in seasons:
            key = str(s).strip() if s is not None else ""
            if key in counts:
                counts[key] += 1
    if not any(counts.values()):
        return []
    top = max(counts.values())
    return [s for s in SEASON_ORDER if counts[s] == top]


def _text(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()


def _list(val: Any) -> list:
    if isinstance(val, list):
        return val
    s = _text(val)
    return [s] if s else []


def load_skus_jsonl(path: str | Path | None = None) -> dict[str, dict]:
    """读取 ``data/processed/skus.jsonl`` → ``{sku_id: 全量记录}``。

    path 缺省取 ``processed_dir() / "skus.jsonl"``（build_catalog.py 产出）。
    """
    p = Path(path) if path else processed_dir() / "skus.jsonl"
    out: dict[str, dict] = {}
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            sid = str(rec.get("sku_id") or "").strip()
            if sid:
                out[sid] = rec
    return out


def _color_block(sid: str, sku: dict) -> dict:
    """从 skus.jsonl 记录重建 color 块（outfit_color_series 回退读 attrName/colorName）。"""
    id_pa = sku.get("id_pa")
    try:
        id_pa = int(id_pa) if id_pa is not None else 0
    except (TypeError, ValueError):
        id_pa = 0
    name = _text(sku.get("attr_name")) or _text(sku.get("color_name")) or None
    return {
        "idPa": id_pa,
        "attrAlias": sid or None,
        "attrName": name,
        "colorName": name,
    }


def build_outfit_item_from_sku(
    sid: str,
    sku: dict | None,
    is_master: bool,
) -> dict:
    """从 skus.jsonl 记录构建 outfit item（SKU 属性单一事实源）。

    字段集与 micro_guide outfit item 对齐：顶层全量属性 + color 块 + attributes
    （sex/upDown/catType/season/series/category_l1/scene_domain）+ images.cover。
    sku 为空（SKU 不在 skus.jsonl）时退化为最小 item，不抛异常。
    """
    sku = sku or {}
    spu_id = _text(sku.get("spu_id"))
    display_image = _text(sku.get("display_image"))
    tryon_image = _text(sku.get("tryon_image")) or display_image
    gender = _list(sku.get("gender"))
    sex = _text(gender[0]) if gender else ""
    season = _list(sku.get("season"))
    cover = tryon_image or display_image
    images: dict[str, Any] = {}
    if cover:
        images["cover"] = cover
    return {
        "sku_id": sid,
        "attrAlias": sid,
        "idAlias": spu_id or None,
        "spu_id": spu_id or None,
        "idGoods": sku.get("id_goods") or sku.get("goods_id"),
        "is_master": is_master,
        "isMaster": is_master,
        "title": _text(sku.get("title")) or None,
        "role": _text(sku.get("role")),
        "category_l1": _text(sku.get("category_l1")) or None,
        "category_l2": _text(sku.get("category_l2")) or None,
        "category_l3": _text(sku.get("category_l3")) or None,
        "series": _text(sku.get("series")) or None,
        "sub_series": _text(sku.get("sub_series")) or None,
        "price": float(sku.get("price") or 0.0),
        "gender": gender,
        "season": season,
        "color_series": _list(sku.get("color_series")),
        "color_name": _text(sku.get("color_name")) or None,
        "color_family": _text(sku.get("color_family")) or None,
        "attr_name": _text(sku.get("attr_name")) or None,
        "scene_domain": _text(sku.get("scene_domain")) or None,
        "length_class": _text(sku.get("length_class")) or None,
        "modeling": _text(sku.get("modeling")) or None,
        "coverage": _text(sku.get("coverage")) or None,
        "layer": _text(sku.get("layer")) or None,
        "is_intimate": bool(sku.get("is_intimate")),
        "occasion_tags": _list(sku.get("occasion_tags")),
        "style_tags": _list(sku.get("style_tags")),
        "search_keywords": _text(sku.get("search_keywords")) or None,
        "search_text": _text(sku.get("search_text")) or None,
        "material": _text(sku.get("material")) or None,
        "fabric_function": _list(sku.get("fabric_function")),
        "age": _text(sku.get("age")) or None,
        "brand": _text(sku.get("brand")) or None,
        "group_brand": _text(sku.get("group_brand")) or None,
        "up_down_raw": _text(sku.get("up_down_raw")) or None,
        "id_pa": sku.get("id_pa"),
        "display_image": display_image or None,
        "tryon_image": tryon_image or None,
        "index_images": _list(sku.get("index_images")),
        "all_images": _list(sku.get("all_images")),
        "color": _color_block(sid, sku),
        "attributes": {
            "sex": sex or None,
            "upDown": _text(sku.get("up_down_raw")) or None,
            "catType": _text(sku.get("category_l1")) or None,
            "season": season,
            "series": _text(sku.get("series")) or None,
            "category_l1": _text(sku.get("category_l1")) or None,
            "scene_domain": _text(sku.get("scene_domain")) or None,
        },
        "images": images,
    }


def dedupe_items_by_sku(items: list[dict]) -> list[dict]:
    """搭配内 item 按 sku_id/attrAlias 去重，保留首次出现顺序。"""
    seen: set[str] = set()
    out: list[dict] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        sid = _text(it.get("sku_id") or it.get("attrAlias"))
        if not sid or sid in seen:
            continue
        seen.add(sid)
        out.append(it)
    return out


def _default_role_of(item: dict) -> str:
    return _text(item.get("role")) if isinstance(item, dict) else ""


def cartesian_split_items_by_role(
    items: list[dict],
    *,
    role_of: Callable[[dict], str] | None = None,
    max_combos: int = 200,
) -> list[list[dict]]:
    """把同一 role 存在多件 SKU 的搭配按笛卡尔积拆成多套（每 role 取 1 件）。

    镜像 ``build_fila_guide_outfits_fast.OutfitBuilder.build_outfit`` 的拆分口径，
    供 dphs / outfits_unique 两个 ETL 在写 ES 前从源头消除"一套搭配里同 role
    多 sku"的情况：按 role 分组 → 各组取 1 件做笛卡尔积 → 每个组合一套。
    不同 role 各 1 件时退化为单套（与旧行为一致，outfit_id 不变）。

    - ``role_of``：取 item role 的回调，缺省读 ``item['role']``；outfits_unique
      传"skus.jsonl role → 文本解析 role"回退，保证鞋类等 role 缺失也能分组。
    - ``max_combos``：组合数上限（默认 200，与 fila_guide 一致），超限按
      per-group cap 截断，避免色款爆炸；截断后部分色款不入库（与 fila_guide
      同口径）。
    - 返回每个组合的 item 列表（浅拷贝、已按 sku 去重、≥2 件）。单 role 或
      拆分后不足 2 件无法形成多 role 搭配，返回 []，调用方据此跳过该行。
    """
    if role_of is None:
        role_of = _default_role_of
    groups: dict[str, list[dict]] = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        groups.setdefault(role_of(it), []).append(it)
    if len(groups) < 2:
        # 单 role（含 role 全空）无法形成多 role 搭配：笛卡尔积只会得到
        # 单件组合，全部 < 2 件，交由下方 len(built) < 2 过滤掉。
        return []
    group_lists = list(groups.values())
    raw_combo_count = 1
    for g in group_lists:
        raw_combo_count *= max(len(g), 1)
    if raw_combo_count > max_combos:
        n_roles = len(group_lists)
        per_group_cap = max(2, int(math.ceil(max_combos ** (1.0 / n_roles))))
        group_lists = [g[:per_group_cap] for g in group_lists]
    combos = list(_iter_product(*group_lists))
    out: list[list[dict]] = []
    for combo in combos:
        built = dedupe_items_by_sku([dict(it) for it in combo])
        if len(built) >= 2:
            out.append(built)
    return out
