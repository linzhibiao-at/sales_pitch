#!/usr/bin/env python3
"""Build data/preview/fila_outfits.json from cc_material_product.csv + tables.

Output schema matches descente/descente_outfits.json for outfits-viewer.

Depends on build_catalog.py output (data/processed/skus.jsonl): every SKU in an
outfit must appear in that on-sale catalog, or the whole outfit is dropped.

Features:
- Requires build_catalog.py output (data/processed/skus.jsonl); drops outfits
  containing any SKU not in the on-sale catalog
- Deduplicates outfits with identical article_no sets, keeping the most recently
  updated one (largest modify_time; ties broken by larger material_id)
- Polars multi-threaded CSV reads (where parsing allows)
- Parallel table loading via ThreadPoolExecutor
- Pre-indexed attr_alias prefix lookup (bisect, avoids full-table scan)
- Parallel outfit assembly via ProcessPoolExecutor (--workers)

Usage::

    python3 scripts/build_fila_guide_outfits_fast.py
    python3 scripts/build_fila_guide_outfits_fast.py --workers 8 --out /tmp/out.json
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, DefaultDict, Dict, List, Optional, Tuple
from itertools import product as iter_product

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts._project_paths import load_paths
from scripts.etl_common import (
    CATEGORY_L2_UP_DOWN,
    _SX_UP_DOWN_MAP,
    infer_role,
    infer_up_down_from_title,
    normalize_gender_to_list,
)
from scripts.outfit_item_builder import aggregate_outfit_season

try:
    import polars as pl
except ImportError as exc:
    raise SystemExit(
        '缺少 polars，请先安装：pip3 install polars '
        '-i https://mirrors.aliyun.com/pypi/simple/',
    ) from exc

_PATHS = load_paths()
PROJECT_ROOT = str(_PATHS['root'])
REPO_ROOT = str(_PATHS['repo_root'])
PRODUCT_DIR = str(_PATHS['product_dir'])
OUT_PATH = str(_PATHS['outfits_json'])

CC_MATERIAL_PRODUCT_BASENAME = 'cc_material_product.csv'
CC_MATERIAL_PRODUCT_PATH = os.path.join(PRODUCT_DIR, CC_MATERIAL_PRODUCT_BASENAME)
PRODUCTS_BRIEF_BASENAME = 'fila_products_brief_prod.xlsx'
SKUS_JSONL_PATH = str(_PATHS['processed_dir'] / 'skus.jsonl')

_POLARS_OPTS = {
    'infer_schema_length': 10000,
    'ignore_errors': True,
    'truncate_ragged_lines': True,
    'encoding': 'utf8-lossy',
}


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _progress(msg: str) -> None:
    sys.stderr.write(msg + '\n')
    sys.stderr.flush()


def norm_id(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    s = str(raw).strip().strip('"').lstrip("'").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def parse_price(val: Any) -> Optional[float]:
    if val is None or val == '':
        return None
    try:
        return float(str(val).strip())
    except ValueError:
        return None


def text_or_empty(val: Any) -> str:
    if val is None:
        return ''
    return str(val).strip()


def first_non_empty(*vals: Any) -> Optional[str]:
    for val in vals:
        txt = text_or_empty(val)
        if txt:
            return txt
    return None


def build_color_block(alias: str, id_pa: int, attr_name: Any) -> Dict[str, Any]:
    name = text_or_empty(attr_name) or None
    alias_s = text_or_empty(alias) or None
    return {
        'idPa': id_pa,
        'attrAlias': alias_s,
        'attrName': name,
        'colorName': name,
    }


def fila_category_l2(ext: Dict[str, str]) -> Optional[str]:
    return first_non_empty(ext.get('middle_class'), ext.get('cat_alias'))


def fila_search_keywords(
    pm: Dict[str, str],
    ext: Dict[str, str],
    alias: str,
) -> Optional[str]:
    st = text_or_empty(pm.get('search_title'))
    if st:
        return st
    kw = text_or_empty(pm.get('keyword'))
    if kw:
        return kw
    season = first_non_empty(ext.get('season'), ext.get('pro_season'))
    bits = [
        text_or_empty(pm.get('id_alias')),
        text_or_empty(pm.get('pro_title')),
        text_or_empty(pm.get('pro_name')),
        text_or_empty(ext.get('sex')),
        text_or_empty(ext.get('series')),
        season,
        text_or_empty(ext.get('cat_alias')),
        text_or_empty(ext.get('middle_class')),
        text_or_empty(ext.get('cat_type')),
        text_or_empty(ext.get('up_down')),
        alias,
        text_or_empty(ext.get('applicable_scenario')),
        text_or_empty(ext.get('functional_tag')),
    ]
    joined = ','.join(b for b in bits if b)
    return joined or None


def item_has_visual(item: Dict[str, Any]) -> bool:
    imgs = item.get('images') or {}
    if imgs.get('cover') or imgs.get('swatch'):
        return True
    if imgs.get('outfitCd'):
        return True
    if imgs.get('outfitCps'):
        return True
    return False


def load_catalog_sku_ids(
    path: str,
) -> tuple[set[str], Dict[str, Dict[str, str]], Dict[str, str], Dict[str, dict]]:
    """读取 build_catalog.py 产出的 skus.jsonl，返回在售 sku_id 集合、图片信息、role 和全量记录。

    Returns:
        (sku_ids, sku_images, sku_role, sku_rows) 其中
        - sku_images 为 {sku_id: {display_image, index_images, tryon_image}}
        - sku_role 为 {sku_id: role}（build_sku_record 已归一化为 top/bottoms/shoes/...，
          比本脚本从原始 ext+title 重新 infer_role 更稳：带 cat_l1 白名单等推断）。
        - sku_rows 为 {sku_id: 全量记录}（build_catalog.py 产出的单一事实源）。
          _build_item_dict 用它覆盖 outfit item 的所有 SKU 属性，保持与 ES skus 索引、
          dphs/unique outfit item 一致；原始表只保留 skus.jsonl 不携带的多图集合等字段。
    """
    if not os.path.isfile(path):
        raise SystemExit(f'缺少 {path}，请先运行 build_catalog.py')
    skus: set[str] = set()
    sku_images: Dict[str, Dict[str, str]] = {}
    sku_role: Dict[str, str] = {}
    sku_rows: Dict[str, dict] = {}
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            sid = str(row.get('sku_id') or '').strip()
            if sid:
                skus.add(sid)
                sku_rows[sid] = row
                role = str(row.get('role') or '').strip()
                if role and role.lower() not in ('n/a', 'unknown'):
                    sku_role[sid] = role
                imgs: Dict[str, str] = {}
                for k in ('display_image', 'tryon_image'):
                    v = str(row.get(k) or '').strip()
                    if v:
                        imgs[k] = v
                # index_images: 取数组第一个非空值作为 fallback cover
                idx_raw = row.get('index_images')
                if isinstance(idx_raw, list):
                    first_url = ''
                    for u in idx_raw:
                        u_s = str(u or '').strip()
                        if u_s:
                            first_url = u_s
                            break
                    if first_url:
                        imgs['index_image'] = first_url
                elif isinstance(idx_raw, str) and idx_raw.strip():
                    # 兼容 JSON 字符串格式的数组
                    try:
                        parsed = json.loads(idx_raw)
                        if isinstance(parsed, list):
                            for u in parsed:
                                u_s = str(u or '').strip()
                                if u_s:
                                    imgs['index_image'] = u_s
                                    break
                    except (ValueError, TypeError):
                        pass
                if imgs:
                    sku_images[sid] = imgs
    return skus, sku_images, sku_role, sku_rows


def dedupe_items_by_sku(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """搭配内 SKU（attrAlias）去重，保留首次出现顺序。"""
    seen: set[str] = set()
    deduped: List[Dict[str, Any]] = []
    for item in items:
        alias = (item.get('attrAlias') or '').strip()
        if not alias or alias in seen:
            continue
        seen.add(alias)
        deduped.append(item)
    return deduped


def _gender_side(ctx: 'BuildContext', gid: int, alias: str) -> Optional[str]:
    """将 SKU 归入性别侧：'男'（男/男童）、'女'（女/女童）或 None（中性/男女同款/未知）。

    用 normalize_gender_to_list 归一化 ext.sex，兼容 男士/女士/男童/女童/中性/男女同款
    等所有写法；sex 为空时回退到 SKU 编码/标题推断。中性或男女同款返回 None，不参与
    多数性别判定，始终保留。
    """
    ext = ctx.exts.get(gid, {})
    pm = ctx.masters.get(gid, {})
    raw_sex = (ext.get('sex') or '').strip()
    title = (pm.get('pro_title') or pm.get('pro_name') or '').strip()
    glist = normalize_gender_to_list(raw_sex, title=title, sku_id=(alias or '').strip())
    if len(glist) == 1:
        g = glist[0]
        if g in ('男', '男童'):
            return '男'
        if g in ('女', '女童'):
            return '女'
    return None


def group_outfit_rows(
    rows: List[Dict[str, Any]],
) -> Tuple[Dict[int, List[Dict[str, Any]]], int]:
    """按 material_id 分组 cc_material_product 行。"""
    by_match: DefaultDict[int, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        mid = norm_id(row.get('material_id'))
        article = text_or_empty(row.get('article_no'))
        name = text_or_empty(row.get('product_name'))
        mpid = norm_id(row.get('material_product_id'))
        if mid is None or not article:
            continue
        by_match[mid].append({
            'material_product_id': mpid or 0,
            'article_no': article,
            'product_name': name,
        })
    for mid in by_match:
        by_match[mid].sort(
            key=lambda x: (x['material_product_id'], x['article_no']),
        )
    return dict(by_match), len(rows)


# ---------------------------------------------------------------------------
# CSV / Polars loaders
# ---------------------------------------------------------------------------

def _read_csv_dicts(path: str) -> List[Dict[str, str]]:
    with open(path, 'r', encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def _load_polars_table(label: str, path: str) -> List[Dict[str, str]]:
    t0 = time.monotonic()
    rows = pl.read_csv(path, **_POLARS_OPTS).to_dicts()
    _progress(
        f'[load] {label}: {len(rows):,} rows ({time.monotonic() - t0:.1f}s, polars)',
    )
    return rows


def _load_csv_table(label: str, path: str) -> List[Dict[str, str]]:
    t0 = time.monotonic()
    rows = _read_csv_dicts(path)
    _progress(
        f'[load] {label}: {len(rows):,} rows ({time.monotonic() - t0:.1f}s, csv)',
    )
    return rows


def _load_table(label: str, path: str, *, use_polars: bool) -> List[Dict[str, str]]:
    _progress(f'[load] {label} ...')
    if use_polars:
        return _load_polars_table(label, path)
    return _load_csv_table(label, path)


# ---------------------------------------------------------------------------
# Build context & outfit builder
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BuildContext:
    masters: Dict[int, Dict[str, str]]
    exts: Dict[int, Dict[str, str]]
    alias_to_gid: Dict[str, int]
    attr_by_goods_alias: Dict[Tuple[int, str], Dict[str, str]]
    color_attrs_by_goods: Dict[int, List[Dict[str, str]]]
    images_by_goods: Dict[int, List[Dict[str, Any]]]
    up_down_by_sku: Dict[str, str]
    sorted_attr_aliases: Tuple[str, ...]
    rows_by_attr_alias: Dict[str, List[Dict[str, str]]]
    by_match: Dict[int, List[Dict[str, Any]]]
    catalog_sku_ids: frozenset[str]
    catalog_sku_images: Dict[str, Dict[str, str]]
    catalog_sku_role: Dict[str, str]
    catalog_sku_rows: Dict[str, dict]


class OutfitBuilder:
    """Stateful builder; one instance per worker process."""

    def __init__(self, ctx: BuildContext) -> None:
        self.ctx = ctx

    @classmethod
    def from_context(cls, ctx: BuildContext) -> 'OutfitBuilder':
        return cls(ctx)

    def _collect_paths(self, gid: int, color_pa: int, image_type: str) -> List[str]:
        rows = [
            row
            for row in self.ctx.images_by_goods.get(gid, [])
            if row['image_type'] == image_type and row['id_pa'] == color_pa
        ]
        rows.sort(key=lambda x: (x['order_id'], x['path']))
        out: List[str] = []
        seen = set()
        for row in rows:
            path = row['path']
            if path in seen:
                continue
            seen.add(path)
            out.append(path)
        return out

    def _first_path_strict_pa(self, gid: int, color_pa: int) -> Optional[str]:
        rows = [r for r in self.ctx.images_by_goods.get(gid, []) if r['id_pa'] == color_pa]
        if not rows:
            return None
        rows.sort(key=lambda x: (x['order_id'], x['path']))
        return rows[0]['path']

    def _pick_cover(self, gid: int, color_pa: int) -> Optional[str]:
        images = self.ctx.images_by_goods.get(gid, [])
        master_rows = [
            r for r in images if r['image_type'] == 'master' and r['id_pa'] == color_pa
        ]
        if master_rows:
            master_rows.sort(key=lambda x: (x['order_id'], x['path']))
            return master_rows[0]['path']
        strict_any = self._first_path_strict_pa(gid, color_pa)
        if strict_any:
            return strict_any
        cands = []
        for row in images:
            if row['image_type'] != 'big':
                continue
            if row['id_pa'] not in (0, color_pa):
                continue
            cands.append((row['id_pa'] == 0, row['order_id'], row['path']))
        if cands:
            cands.sort(key=lambda x: (0 if x[0] else 1, x[1]))
            return cands[0][2]
        pm = self.ctx.masters.get(gid, {})
        if pm and (pm.get('image') or '').strip():
            return (pm.get('image') or '').strip()
        return None

    def _pick_left_hero_strict_pi(self, gid: int, color_pa: int) -> Optional[str]:
        return self._first_path_strict_pa(gid, color_pa)

    def _build_item_dict(
        self,
        gid: int,
        alias: str,
        is_master: bool,
        pa: Optional[Dict[str, str]],
    ) -> Dict[str, Any]:
        id_pa = norm_id(pa.get('id_pa')) if pa else None
        if id_pa is None:
            id_pa = 0
        attr_name = ((pa or {}).get('attr_name') or '').strip() or None
        swatch = ((pa or {}).get('image_url') or '').strip() or None
        pm = self.ctx.masters.get(gid, {})
        ext = self.ctx.exts.get(gid, {})
        cover = self._pick_cover(gid, id_pa)
        cd_list = self._collect_paths(gid, id_pa, 'cd')
        cd2_list = self._collect_paths(gid, id_pa, 'cd2')
        price = parse_price(pm.get('price'))
        season = first_non_empty(ext.get('season'), ext.get('pro_season'))
        title = first_non_empty(pm.get('pro_title'), pm.get('pro_name')) or ''
        cat_l2 = fila_category_l2(ext)
        up_down = first_non_empty(
            ext.get('up_down'),
            self.ctx.up_down_by_sku.get((alias or '').strip()),
        )
        up_down = _SX_UP_DOWN_MAP.get(up_down, up_down)
        if not up_down and cat_l2:
            up_down = CATEGORY_L2_UP_DOWN.get(cat_l2, '')
        if not up_down:
            up_down = infer_up_down_from_title(title)
        item = {
            'attrAlias': alias,
            # ``alias`` is the full goods code (e.g. F13W623162FBK) — the same
            # key the sku embedding cache is built on. Populate ``sku_id`` so
            # downstream consumers (scoring /score, card builder, ...) don't
            # have to fall back through attrAlias; legacy code read sku_id=None
            # here and missed the precomputed embedding cache.
            'sku_id': alias,
            'isMaster': is_master,
            'idGoods': gid,
            'idAlias': (pm.get('id_alias') or '').strip() or None,
            'title': title or None,
            'category_l2': cat_l2,
            'series': first_non_empty(ext.get('series')),
            'search_keywords': fila_search_keywords(pm, ext, alias),
            'price': price,
            'color': build_color_block(alias, id_pa, attr_name),
            'attributes': {
                'sex': first_non_empty(ext.get('sex')),
                'upDown': up_down or None,
                'catType': first_non_empty(ext.get('cat_type')),
                'season': season or None,
                'series': first_non_empty(ext.get('series')),
            },
            'images': {
                'cover': cover,
                'swatch': swatch,
                'outfitCd': cd_list,
                'outfitCps': cd2_list,
            },
        }
        # ── SKU 属性统一覆盖为 skus.jsonl（build_catalog.py 产出）值 ──────────
        # 单一事实源：与 ES skus 索引、dphs/unique outfit item 保持一致。原始表只保留
        # skus.jsonl 不携带的字段（images.outfitCd/outfitCps 多图集合、swatch、idGoods、
        # isMaster）。非空才覆盖，缺失回退原始表派生值，保证 catalog 缺该 SKU 时不退化。
        catalog = self.ctx.catalog_sku_rows.get((alias or '').strip())
        if catalog:
            def _pick(key: str, cast: type = str) -> Any:
                v = catalog.get(key)
                if v is None:
                    return None
                if cast is list:
                    return v if (isinstance(v, list) and v) else None
                if isinstance(v, str):
                    s = v.strip()
                    return s if s else None
                return v
            # 顶层标量属性
            for k in ('title', 'role', 'category_l1', 'category_l2', 'category_l3',
                      'series', 'sub_series', 'price', 'scene_domain', 'length_class',
                      'modeling', 'coverage', 'layer', 'is_intimate', 'search_keywords',
                      'search_text', 'material', 'fabric_function', 'age', 'brand',
                      'up_down_raw', 'attr_name', 'color_name', 'color_family',
                      'display_image', 'tryon_image', 'id_pa', 'spu_id'):
                val = _pick(k)
                if val is not None:
                    item[k] = val
            spu = _pick('spu_id')
            if spu:
                item['idAlias'] = spu
            gid_cat = catalog.get('id_goods') or catalog.get('goods_id')
            if gid_cat is not None:
                item['id_goods'] = gid_cat
            # 列表属性
            for k in ('gender', 'season', 'color_series', 'occasion_tags',
                      'style_tags', 'index_images', 'all_images'):
                val = _pick(k, list)
                if val is not None:
                    item[k] = val
            # color 块从 catalog 重建（item_color_series 回退读 color.attrName/colorName）
            pa_val = norm_id(catalog.get('id_pa'))
            if pa_val is None:
                pa_val = 0
            item['color'] = build_color_block(
                alias, pa_val,
                catalog.get('attr_name') or catalog.get('color_name'),
            )
            # attributes 同步：sex 回退用 gender 首元素；upDown 用 up_down_raw；
            # catType 保留原始表（skus.jsonl 无）；season/series/category_l1/scene_domain 同步
            attrs = dict(item.get('attributes') or {})
            glist = catalog.get('gender')
            if isinstance(glist, list) and glist:
                attrs['sex'] = str(glist[0] or '').strip() or attrs.get('sex')
            ud = _pick('up_down_raw')
            if ud is not None:
                attrs['upDown'] = ud
            season_cat = _pick('season', list)
            if season_cat is not None:
                attrs['season'] = season_cat
            for ak, ck in (('series', 'series'), ('category_l1', 'category_l1'),
                           ('scene_domain', 'scene_domain')):
                cv = _pick(ck)
                if cv is not None:
                    attrs[ak] = cv
            item['attributes'] = attrs
            # images.cover 优先 select_images.py 精选图
            cover = _pick('tryon_image') or _pick('display_image')
            if cover:
                item['images']['cover'] = cover
        else:
            # catalog 缺该 SKU（理论不会发生，outfit_all_in_catalog 已门禁）：回退旧逻辑
            cat_imgs = self.ctx.catalog_sku_images.get((alias or '').strip())
            if cat_imgs:
                if cat_imgs.get('tryon_image'):
                    item['images']['cover'] = cat_imgs['tryon_image']
                if cat_imgs.get('display_image') and not item['images']['cover']:
                    item['images']['cover'] = cat_imgs['display_image']
            catalog_role = self.ctx.catalog_sku_role.get((alias or '').strip())
            item['role'] = catalog_role or infer_role(ext, title, up_down) or ''
        return item

    @staticmethod
    def _row_score(
        a: str, row: Dict[str, str], masters: Dict[int, Dict[str, str]],
    ) -> Tuple[int, int, int, str]:
        al = (row.get('attr_alias') or '').strip()
        gid = norm_id(row.get('id_goods')) or 0
        pm = masters.get(gid, {})
        mid = (pm.get('id_alias') or '').strip()
        exact = 0 if al == a else 1
        master_ok = 0 if mid == a else 1
        oid = norm_id(row.get('order_id')) or 0
        return (exact, master_ok, oid, al)

    def _candidates_for_article(self, article: str) -> List[Dict[str, str]]:
        aliases = self.ctx.sorted_attr_aliases
        idx = bisect.bisect_left(aliases, article)
        cands: List[Dict[str, str]] = []
        while idx < len(aliases) and aliases[idx].startswith(article):
            cands.extend(self.ctx.rows_by_attr_alias[aliases[idx]])
            idx += 1
        return cands

    def _resolve_all_skus_for_article(
        self, article: str,
    ) -> List[Tuple[int, str]]:
        """Resolve an article_no to ALL matching (gid, alias) SKU pairs."""
        a = (article or '').strip()
        if not a:
            return []
        gid = self.ctx.alias_to_gid.get(a)
        if gid is not None:
            rows = self.ctx.color_attrs_by_goods.get(gid, [])
            result = []
            for r in rows:
                al = (r.get('attr_alias') or '').strip()
                if al:
                    result.append((gid, al))
            return result if result else [(gid, a)]
        cands = self._candidates_for_article(a)
        if not cands:
            return []
        seen_gids: set = set()
        result = []
        for cand in sorted(
            cands, key=lambda r: self._row_score(a, r, self.ctx.masters),
        ):
            g = norm_id(cand.get('id_goods'))
            al = (cand.get('attr_alias') or '').strip()
            if g is None or not al:
                continue
            if g in seen_gids:
                continue
            seen_gids.add(g)
            result.append((g, al))
        return result

    def resolve_article(self, article: str) -> Tuple[Optional[int], str]:
        a = (article or '').strip()
        if not a:
            return None, ''
        gid = self.ctx.alias_to_gid.get(a)
        if gid is not None:
            rows = self.ctx.color_attrs_by_goods.get(gid, [])
            pref = [r for r in rows if (r.get('attr_alias') or '').startswith(a)]
            pool = pref if pref else rows
            if not pool:
                return gid, a
            best = min(pool, key=lambda r: self._row_score(a, r, self.ctx.masters))
            alias = (best.get('attr_alias') or '').strip() or a
            return gid, alias
        cands = self._candidates_for_article(a)
        if not cands:
            return None, ''
        best = min(cands, key=lambda r: self._row_score(a, r, self.ctx.masters))
        gid = norm_id(best.get('id_goods'))
        if gid is None:
            return None, ''
        alias = (best.get('attr_alias') or '').strip() or a
        return gid, alias

    def outfit_all_in_catalog(self, mid: int) -> bool:
        for row in self.ctx.by_match[mid]:
            gid, alias = self.resolve_article(row['article_no'])
            if gid is None or not alias or alias not in self.ctx.catalog_sku_ids:
                return False
        return True

    def _assemble_outfit_dict(
        self,
        mid: int,
        items: List[Dict[str, Any]],
        rows: List[Dict[str, Any]],
        outfit_suffix: int = 0,
    ) -> Dict[str, Any]:
        """Assemble a single outfit dict from a list of built item dicts."""
        for it in items:
            it['isMaster'] = False
        items[0]['isMaster'] = True

        first_row_name = (rows[0].get('product_name') or '').strip()
        master_item = items[0]
        master_title = (master_item.get('title') or '').strip()
        display_name = master_title or first_row_name or f'搭配 {mid}'
        id_shop = norm_id(
            self.ctx.masters.get(master_item['idGoods'], {}).get('id_shop'),
        )
        if id_shop is None:
            id_shop = 7

        left_hero_url = None
        col = master_item.get('color') or {}
        ipa = col.get('idPa')
        if ipa is not None and master_item.get('idGoods') is not None:
            left_hero_url = self._pick_left_hero_strict_pi(
                int(master_item['idGoods']),
                int(ipa),
            )

        outfit_id = mid if outfit_suffix == 0 else -(mid * 10000 + outfit_suffix)
        return {
            'idMatch': outfit_id,
            'name': display_name,
            'idShop': id_shop,
            'shopName': 'FILA 微导购',
            'type': 1,
            'backgroundImg': None,
            'leftHeroUrl': left_hero_url,
            # 季节标签：按搭配内 item.season 多数投票（并列全取，按春夏秋冬排序），
            # 与 outfits_unique / dphs_outfits 同口径；build_fila_es_index.outfit_doc 读此字段入 ES。
            'season': aggregate_outfit_season(items),
            'flags': {
                'hasPdpOutfitImage': bool(
                    (master_item.get('images') or {}).get('outfitCd'),
                ),
                'hasCpsOutfitImage': bool(
                    (master_item.get('images') or {}).get('outfitCps'),
                ),
            },
            'items': items,
        }

    def build_outfit(self, mid: int) -> List[Dict[str, Any]]:
        """Build outfit(s) for a material_id.

        Returns a list: usually one outfit, but when multiple SKUs share the
        same role the outfit is split via Cartesian product into multiple.
        Also filters out minority-gender SKUs when both 男 and 女 are present.
        """
        if not self.outfit_all_in_catalog(mid):
            return []
        rows = self.ctx.by_match[mid]

        # 1. Resolve each article to ALL matching SKUs
        all_sku_pairs: List[Tuple[int, str]] = []
        for row in rows:
            skus = self._resolve_all_skus_for_article(row['article_no'])
            for gid, alias in skus:
                if alias in self.ctx.catalog_sku_ids:
                    all_sku_pairs.append((gid, alias))

        # 2. Filter minority gender when both 男 and 女 coexist
        all_sku_pairs = self._filter_minority_gender(all_sku_pairs, self.ctx)

        # 2b. 搭配内 SKU 去重
        seen_aliases: set[str] = set()
        deduped_pairs: List[Tuple[int, str]] = []
        for gid, alias in all_sku_pairs:
            if alias in seen_aliases:
                continue
            seen_aliases.add(alias)
            deduped_pairs.append((gid, alias))
        all_sku_pairs = deduped_pairs

        # 3. Build items and compute roles (with SX mapping applied)
        items_with_role: List[Tuple[Dict[str, Any], str]] = []
        for gid, alias in all_sku_pairs:
            pa = self.ctx.attr_by_goods_alias.get((gid, alias), {})
            item = self._build_item_dict(gid, alias, False, pa or None)
            if not item_has_visual(item):
                continue
            ext = self.ctx.exts.get(gid, {})
            title = (item.get('title') or '').strip()
            # role 已在 _build_item_dict 复用 skus.jsonl 归一化 role；此处仅兜底重推。
            role = item.get('role') or infer_role(
                ext, title, item.get('attributes', {}).get('upDown') or '',
            )
            items_with_role.append((item, role))

        # Dress demotion: if any SKU has role 'bottoms', demote 'dress' to 'top'
        has_bottoms = any(r == 'bottoms' for _, r in items_with_role)
        role_groups: DefaultDict[str, List[Dict[str, Any]]] = defaultdict(list)
        for item, role in items_with_role:
            if role == 'dress' and has_bottoms:
                role = 'top'
            # 持久化推断 role 到 item：ES 索引 _item_role 优先取此字段，避免
            # 鞋类（upDown 为空）等角色回退为空，导致下游 target_role 覆盖检查
            # 误判缺角色而丢弃整套（anchor_graph 通路 0 召回）。
            item['role'] = role
            role_groups[role].append(item)

        roles = sorted(role_groups.keys())
        if len(roles) < 2:
            return []

        # 4. Cartesian product over role groups
        group_lists = [role_groups[r] for r in roles]
        MAX_COMBOS = 200
        raw_combo_count = 1
        for g in group_lists:
            raw_combo_count *= max(len(g), 1)
        if raw_combo_count > MAX_COMBOS:
            n_roles = len(group_lists)
            per_group_cap = max(2, int(math.ceil(MAX_COMBOS ** (1.0 / n_roles))))
            for i, g in enumerate(group_lists):
                if len(g) > per_group_cap:
                    group_lists[i] = g[:per_group_cap]
        combos = list(iter_product(*group_lists))

        outfits: List[Dict[str, Any]] = []
        for idx, combo in enumerate(combos):
            built = dedupe_items_by_sku([dict(it) for it in combo])
            if len(built) < 2:
                continue
            outfit = self._assemble_outfit_dict(mid, built, rows, outfit_suffix=idx)
            outfits.append(outfit)

        return outfits


    @staticmethod
    def _filter_minority_gender(
        sku_pairs: List[Tuple[int, str]],
        ctx: BuildContext,
    ) -> List[Tuple[int, str]]:
        """若搭配内同时存在男系与女系商品，丢弃少数性别侧的 SKU。

        男系={男,男童}，女系={女,女童}；中性/男女同款/未知不计入，始终保留。
        sex 经 normalize_gender_to_list 归一化，兼容 男士/女士/男童/女童 等写法。
        两侧计数相等时保留男系（确定性裁剪），避免搭配仍混合。
        """
        side_by_sku: Dict[Tuple[int, str], Optional[str]] = {}
        counts: DefaultDict[str, int] = defaultdict(int)
        for gid, alias in sku_pairs:
            side = _gender_side(ctx, gid, alias)
            side_by_sku[(gid, alias)] = side
            if side in ('男', '女'):
                counts[side] += 1
        if counts.get('男', 0) == 0 or counts.get('女', 0) == 0:
            return sku_pairs
        majority = '男' if counts['男'] >= counts['女'] else '女'
        minority = '女' if majority == '男' else '男'
        return [
            (gid, alias)
            for gid, alias in sku_pairs
            if side_by_sku.get((gid, alias)) != minority
        ]


# ---------------------------------------------------------------------------
# Worker process helpers
# ---------------------------------------------------------------------------

_WORKER_BUILDER: Optional[OutfitBuilder] = None


def _init_worker(ctx: BuildContext) -> None:
    global _WORKER_BUILDER
    _WORKER_BUILDER = OutfitBuilder.from_context(ctx)


def _build_chunk(mids: List[int]) -> Tuple[List[Dict[str, Any]], int, int]:
    assert _WORKER_BUILDER is not None
    outfits: List[Dict[str, Any]] = []
    skipped_not_onsell = 0
    skipped_lt2 = 0
    for mid in mids:
        if not _WORKER_BUILDER.outfit_all_in_catalog(mid):
            skipped_not_onsell += 1
            continue
        outfit_list = _WORKER_BUILDER.build_outfit(mid)
        if not outfit_list:
            skipped_lt2 += 1
        else:
            outfits.extend(outfit_list)
    return outfits, skipped_not_onsell, skipped_lt2


def _chunk_ids(ids: List[int], workers: int) -> List[List[int]]:
    if workers <= 1:
        return [ids]
    chunk_size = max(1, (len(ids) + workers - 1) // workers)
    return [ids[i:i + chunk_size] for i in range(0, len(ids), chunk_size)]


# ---------------------------------------------------------------------------
# Index building
# ---------------------------------------------------------------------------

def _build_indexes(product_dir: str) -> BuildContext:
    table_specs = [
        ('product_master.csv', False),
        ('product_master_ext.csv', False),
        ('product_attr.csv', True),
        ('product_image.csv', True),
    ]
    loaded: Dict[str, List[Dict[str, str]]] = {}

    with ThreadPoolExecutor(max_workers=len(table_specs)) as pool:
        futures = {
            pool.submit(
                _load_table,
                name,
                os.path.join(product_dir, name),
                use_polars=use_polars,
            ): name
            for name, use_polars in table_specs
        }
        for future in as_completed(futures):
            name = futures[future]
            loaded[name] = future.result()

    masters: Dict[int, Dict[str, str]] = {}
    alias_to_gid: Dict[str, int] = {}
    for row in loaded['product_master.csv']:
        gid = norm_id(row.get('id_goods'))
        if gid is None:
            continue
        masters[gid] = row
        alias = (row.get('id_alias') or '').strip()
        if alias and alias not in alias_to_gid:
            alias_to_gid[alias] = gid

    exts: Dict[int, Dict[str, str]] = {}
    for row in loaded['product_master_ext.csv']:
        gid = norm_id(row.get('id_goods'))
        if gid is not None:
            exts[gid] = row

    attr_by_goods_alias: Dict[Tuple[int, str], Dict[str, str]] = {}
    color_attrs_by_goods: DefaultDict[int, List[Dict[str, str]]] = defaultdict(list)
    rows_by_attr_alias: DefaultDict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in loaded['product_attr.csv']:
        gid = norm_id(row.get('id_goods'))
        if gid is None:
            continue
        alias = (row.get('attr_alias') or '').strip()
        if alias:
            attr_by_goods_alias[(gid, alias)] = row
        if str(row.get('id_pac', '')).strip() != '1':
            continue
        if str(row.get('status', '0')).strip() != '0':
            continue
        color_attrs_by_goods[gid].append(row)
        if alias:
            rows_by_attr_alias[alias].append(row)

    for gid in color_attrs_by_goods:
        color_attrs_by_goods[gid].sort(
            key=lambda x: (
                norm_id(x.get('order_id')) or 0,
                (x.get('attr_alias') or '').strip(),
            ),
        )

    sorted_attr_aliases = tuple(sorted(rows_by_attr_alias.keys()))

    images_by_goods: DefaultDict[int, List[Dict[str, Any]]] = defaultdict(list)
    for row in loaded['product_image.csv']:
        gid = norm_id(row.get('id_goods'))
        if gid is None:
            continue
        if str(row.get('status', '0')).strip() != '1':
            continue
        path = (row.get('path') or '').strip()
        if not path:
            continue
        id_pa = norm_id(row.get('id_pa'))
        if id_pa is None:
            id_pa = 0
        order_id = norm_id(row.get('order_id'))
        if order_id is None:
            order_id = 0
        images_by_goods[gid].append({
            'id_pa': id_pa,
            'image_type': (row.get('image_type') or '').strip(),
            'order_id': order_id,
            'path': path,
        })

    return BuildContext(
        masters=masters,
        exts=exts,
        alias_to_gid=alias_to_gid,
        attr_by_goods_alias=attr_by_goods_alias,
        color_attrs_by_goods=dict(color_attrs_by_goods),
        images_by_goods=dict(images_by_goods),
        up_down_by_sku={},
        sorted_attr_aliases=sorted_attr_aliases,
        rows_by_attr_alias=dict(rows_by_attr_alias),
        by_match={},
        catalog_sku_ids=frozenset(),
        catalog_sku_images={},
        catalog_sku_role={},
        catalog_sku_rows={},
    )


# ---------------------------------------------------------------------------
# Outfit group loading & deduplication
# ---------------------------------------------------------------------------

def _deduplicate_outfits(
    by_match: Dict[int, List[Dict[str, Any]]],
    max_time_by_mid: Dict[int, str],
) -> Tuple[Dict[int, List[Dict[str, Any]]], int]:
    """按 article_no 集合去重，相同搭配保留 modify_time 最新的（相同则取较大 material_id）。"""
    seen: Dict[frozenset, int] = {}
    deduped: Dict[int, List[Dict[str, Any]]] = {}
    dup_count = 0
    for mid in sorted(by_match.keys()):
        sig = frozenset(row['article_no'] for row in by_match[mid])
        if sig not in seen:
            seen[sig] = mid
            deduped[mid] = by_match[mid]
            continue
        prev_mid = seen[sig]
        cur_time = max_time_by_mid.get(mid, '')
        prev_time = max_time_by_mid.get(prev_mid, '')
        if cur_time >= prev_time:
            del deduped[prev_mid]
            deduped[mid] = by_match[mid]
            seen[sig] = mid
        dup_count += 1
    return deduped, dup_count


def _load_cc_material_groups(
    csv_path: str,
) -> Tuple[Dict[int, List[Dict[str, Any]]], int]:
    if not os.path.isfile(csv_path):
        raise SystemExit(f'找不到搭配 CSV：{csv_path}')
    _progress(f'[load] {CC_MATERIAL_PRODUCT_BASENAME} ...')
    t0 = time.monotonic()
    rows = pl.read_csv(csv_path, **_POLARS_OPTS).to_dicts()
    _progress(
        f'[load] {CC_MATERIAL_PRODUCT_BASENAME}: {len(rows):,} rows '
        f'({time.monotonic() - t0:.1f}s, polars)',
    )
    # 每个 material_id 取所有行中最大的 modify_time，用于去重仲裁
    max_time_by_mid: Dict[int, str] = {}
    for row in rows:
        mid = norm_id(row.get('material_id'))
        if mid is None:
            continue
        t = str(row.get('modify_time') or '').strip()
        if t > max_time_by_mid.get(mid, ''):
            max_time_by_mid[mid] = t
    by_match, row_count = group_outfit_rows(rows)
    by_match, dup_count = _deduplicate_outfits(by_match, max_time_by_mid)
    if dup_count:
        _progress(f'[dedup] 共去除 {dup_count:,} 个重复搭配（article_no 集合相同）')
    return by_match, row_count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--product-dir',
        default=PRODUCT_DIR,
        help='商品 CSV 目录，默认 config paths.product_dir (data/tables)',
    )
    parser.add_argument(
        '--cc-material-csv',
        default=CC_MATERIAL_PRODUCT_PATH,
        help='搭配 CSV 路径，默认 product_dir/cc_material_product.csv',
    )
    parser.add_argument(
        '--out',
        default=OUT_PATH,
        help='输出 JSON 路径，默认 data/preview/fila_outfits.json',
    )
    parser.add_argument(
        '--skus-jsonl',
        default=SKUS_JSONL_PATH,
        help='在售 SKU 目录，默认 data/processed/skus.jsonl（build_catalog.py 产出）',
    )
    parser.add_argument(
        '--workers',
        type=int,
        default=max(1, (os.cpu_count() or 4) - 1),
        help='并行构建进程数，默认 CPU 核数 - 1',
    )
    args = parser.parse_args()

    product_dir = args.product_dir
    cc_material_csv = args.cc_material_csv
    out_path = args.out
    skus_jsonl = args.skus_jsonl
    workers = max(1, args.workers)

    if not os.path.isdir(product_dir):
        raise SystemExit(f'找不到商品目录：{product_dir}')

    _progress(f'[start] product_dir={product_dir}')
    _progress(f'[start] cc_material_csv={cc_material_csv}')
    _progress(f'[start] out={out_path}')
    _progress(f'[start] skus_jsonl={skus_jsonl}')
    _progress(f'[start] workers={workers}')

    _progress('[load] skus.jsonl (on-sale catalog) ...')
    t0 = time.monotonic()
    catalog_skus, catalog_sku_images, catalog_sku_role, catalog_sku_rows = load_catalog_sku_ids(skus_jsonl)
    _progress(
        f'[load] skus.jsonl: {len(catalog_skus):,} on-sale skus, '
        f'{len(catalog_sku_images):,} with images, '
        f'{len(catalog_sku_role):,} with role '
        f'({time.monotonic() - t0:.1f}s)',
    )

    tools_dir = str(_PATHS['tools_dir'])
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import product_index_gallery

    products_brief_xlsx = os.path.join(product_dir, PRODUCTS_BRIEF_BASENAME)
    if os.path.isfile(products_brief_xlsx):
        _progress(f'[load] {PRODUCTS_BRIEF_BASENAME} (up_down) ...')
    t0 = time.monotonic()
    up_down_by_sku = product_index_gallery.load_sku_up_down_map(
        products_brief_xlsx if os.path.isfile(products_brief_xlsx) else None,
    )
    if os.path.isfile(products_brief_xlsx):
        _progress(
            f'[load] {PRODUCTS_BRIEF_BASENAME}: '
            f'{len(up_down_by_sku):,} sku '
            f'({time.monotonic() - t0:.1f}s)',
        )

    t_index = time.monotonic()
    ctx = _build_indexes(product_dir)
    by_match, cc_row_count = _load_cc_material_groups(cc_material_csv)
    ctx = BuildContext(
        masters=ctx.masters,
        exts=ctx.exts,
        alias_to_gid=ctx.alias_to_gid,
        attr_by_goods_alias=ctx.attr_by_goods_alias,
        color_attrs_by_goods=ctx.color_attrs_by_goods,
        images_by_goods=ctx.images_by_goods,
        up_down_by_sku=up_down_by_sku,
        sorted_attr_aliases=ctx.sorted_attr_aliases,
        rows_by_attr_alias=ctx.rows_by_attr_alias,
        by_match=by_match,
        catalog_sku_ids=frozenset(catalog_skus),
        catalog_sku_images=catalog_sku_images,
        catalog_sku_role=catalog_sku_role,
        catalog_sku_rows=catalog_sku_rows,
    )
    _progress(
        f'[index] tables + guide ready in {time.monotonic() - t_index:.1f}s',
    )
    _progress(
        f'[group] cc_material_product: {cc_row_count:,} rows -> '
        f'{len(by_match):,} outfits',
    )

    match_ids = sorted(by_match.keys())
    total_matches = len(match_ids)
    chunks = _chunk_ids(match_ids, workers)
    _progress(
        f'[build] processing {total_matches:,} outfits '
        f'({len(chunks)} chunks, workers={workers}) ...',
    )
    t_build = time.monotonic()

    outfits: List[Dict[str, Any]] = []
    skipped_not_onsell = 0
    skipped_lt2 = 0

    if workers == 1:
        builder = OutfitBuilder.from_context(ctx)
        report_every = max(1, total_matches // 20)
        for i, mid in enumerate(match_ids):
            if not builder.outfit_all_in_catalog(mid):
                skipped_not_onsell += 1
                continue
            outfit_list = builder.build_outfit(mid)
            if not outfit_list:
                skipped_lt2 += 1
            else:
                outfits.extend(outfit_list)
            done = i + 1
            if done % report_every == 0 or done == total_matches:
                pct = 100.0 * done / total_matches
                _progress(
                    f'[build] {done:,}/{total_matches:,} outfits '
                    f'({pct:.0f}%, wrote {len(outfits):,})',
                )
    else:
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_worker,
            initargs=(ctx,),
        ) as pool:
            for chunk_outfits, chunk_not_onsell, chunk_lt2 in pool.map(
                _build_chunk,
                chunks,
            ):
                outfits.extend(chunk_outfits)
                skipped_not_onsell += chunk_not_onsell
                skipped_lt2 += chunk_lt2

    build_elapsed = time.monotonic() - t_build
    _progress(
        f'[build] done in {build_elapsed:.1f}s: {len(outfits):,} outfits, '
        f'skipped_not_onsell={skipped_not_onsell:,}, '
        f'skipped_lt2={skipped_lt2:,}',
    )

    outfits.sort(key=lambda x: int(x['idMatch']), reverse=True)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    _progress(f'[write] {out_path} ...')
    t_write = time.monotonic()
    with open(out_path, 'w', encoding='utf-8') as handle:
        json.dump(outfits, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    write_elapsed = time.monotonic() - t_write
    out_size = os.path.getsize(out_path)
    _progress(
        f'[write] done: {out_size / (1024 * 1024):.1f} MiB ({write_elapsed:.1f}s)',
    )

    sys.stdout.write(
        f'Wrote {len(outfits)} outfits to {out_path}\n'
        f'  skipped (not all on-sale in skus.jsonl): {skipped_not_onsell}\n'
        f'  skipped (usable items < 2): {skipped_lt2}\n'
        f'  unique material_id in csv: {len(by_match)}\n',
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
