#!/usr/bin/env python3
"""Build fila_outfits.json in the same shape as descente/descente_outfits.json.

Reads GB18030 CSV exports（`--product-csv` / `--template-csv`，默认在 `data/tables/`）:
- 斐乐官网商品.csv: SKU master data (款号 / 货号 / 属性 / 价格等)
- 搭配模板.csv: optional outfit templates (主货号 + 逗号分隔货号)
- product_image.csv (UTF-8-SIG): id_goods / path / image_type / status，用于补全
  images.cover、outfitCd、outfitCps 等（与迪桑特 JSON 对齐）

Two modes:
- join: only rows whose 店铺 matches --shop-substring (斐乐模板导出后可用)
- demo-style: one outfit per 款号，聚合该款下不同颜色货号（当前仓库可用）

当前数据说明：随仓库自带的 搭配模板.csv 全为「迪桑特小程序」且货号体系
与斐乐 T11* 货号无交集；在拿到斐乐搭配模板前，请用 demo-style 预览前端。

微导购统一流水线请使用 `scripts/build_fila_guide_outfits_fast.py`
生成 `data/preview/fila_outfits.json`，再运行 `scripts/build_fila_es_index.py`
写入 ES；本脚本仅保留为 legacy/demo 数据转换工具。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, DefaultDict, Dict, List, Optional, Set, Tuple

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


EMPTY_IMG = 'goods_empty.png'


def _parse_template_skus(cell: str) -> List[str]:
    if not cell:
        return []
    text = cell.strip().strip('"')
    parts = re.split(r'[,，]\s*', text)
    return [p.strip() for p in parts if p.strip()]


def _parse_bool_cn(value: str) -> bool:
    v = (value or '').strip()
    return v in ('是', 'Y', 'y', 'true', 'True', '1')


def _infer_up_down(row: Dict[str, str]) -> str:
    cat = (row.get('品类') or '').strip()
    big = (row.get('商品大类') or '').strip()
    if '鞋' in cat or big == '鞋':
        return 'N/A'
    if '帽' in cat or '袜' in cat or '包' in cat or big == '配件':
        return 'N/A'
    lower_like = ('裤' in cat) or ('裤' in (row.get('筛选品类') or ''))
    upper_like = any(
        x in cat
        for x in ('T', 't', '衣', '外套', '夹克', '卫衣', '针织衫', '衬衫')
    )
    if lower_like and not upper_like:
        return '下装'
    if upper_like and not lower_like:
        return '上装'
    return 'N/A'


def _stable_id_from_text(text: str) -> int:
    """Deterministic positive int for idMatch / idGoods placeholders."""
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    value = int(digest[:12], 16)
    return int(value % 9_000_000_000) + 1_000_000_000


def _read_products(path: str) -> Tuple[Dict[str, Dict[str, str]], Dict[str, List[str]]]:
    """Return (by_sku, style_to_skus)."""
    by_sku: Dict[str, Dict[str, str]] = {}
    style_to_skus: Dict[str, List[str]] = {}
    with open(path, 'r', encoding='gb18030', newline='') as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            sku = (row.get('货号') or '').strip()
            style = (row.get('款号') or '').strip()
            if not sku:
                continue
            if sku not in by_sku:
                by_sku[sku] = row
            style_to_skus.setdefault(style, []).append(sku)
    for style, skus in style_to_skus.items():
        style_to_skus[style] = sorted(set(skus))
    return by_sku, style_to_skus


def _read_sku_id_goods_map(path: str) -> Dict[str, str]:
    """CSV: 货号 + id_goods（表头可为 id_goods 或 idGoods），UTF-8-SIG 或 GB18030。"""
    out: Dict[str, str] = {}
    for enc in ('utf-8-sig', 'gb18030'):
        try:
            with open(path, 'r', encoding=enc, newline='') as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    sku = (row.get('货号') or row.get('sku') or '').strip()
                    gid = (
                        row.get('id_goods')
                        or row.get('idGoods')
                        or ''
                    ).strip()
                    if sku and gid:
                        out[sku] = gid
            return out
        except UnicodeDecodeError:
            continue
    return out


def _read_product_image_rows(path: str) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    with open(path, 'r', encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if (row.get('status') or '').strip() != '1':
                continue
            url = (row.get('path') or '').strip()
            if not url or EMPTY_IMG in url:
                continue
            rows.append(row)
    return rows


def _order_key(row: Dict[str, str]) -> Tuple[int, str]:
    raw = (row.get('order_id') or '0').strip()
    try:
        return (int(raw), row.get('path') or '')
    except ValueError:
        return (0, row.get('path') or '')


def _build_sku_image_buckets(
    image_rows: List[Dict[str, str]],
    catalog_skus: Set[str],
    style_to_skus: Dict[str, List[str]],
    sku_to_id_goods: Optional[Dict[str, str]] = None,
) -> Dict[str, Dict[str, List[str]]]:
    """Map attrAlias (货号) -> image_type -> ordered unique URLs."""
    raw: DefaultDict[str, DefaultDict[str, List[Dict[str, str]]]] = defaultdict(
        lambda: defaultdict(list),
    )
    catalog_styles = {s for s in style_to_skus if s}

    groups: DefaultDict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in image_rows:
        gid = (row.get('id_goods') or '').strip()
        if gid:
            groups[gid].append(row)

    def append_row(sku: str, itype: str, row: Dict[str, str]) -> None:
        url = (row.get('path') or '').strip()
        if not url:
            return
        lst = raw[sku][itype]
        if any((r.get('path') or '').strip() == url for r in lst):
            return
        lst.append(row)

    def skus_matching_path(path: str) -> List[str]:
        return [s for s in catalog_skus if s and s in path]

    for row in image_rows:
        path = row.get('path') or ''
        itype = (row.get('image_type') or '').strip() or 'other'
        id_pa = (row.get('id_pa') or '').strip()
        for sku in skus_matching_path(path):
            append_row(sku, itype, row)

        for style in catalog_styles:
            if not style or len(style) < 8:
                continue
            if style not in path:
                continue
            if id_pa == '0' and itype == 'big':
                for sku in style_to_skus.get(style, []):
                    if sku in catalog_skus:
                        append_row(sku, itype, row)

    if sku_to_id_goods:
        for sku, gid in sku_to_id_goods.items():
            if sku not in catalog_skus:
                continue
            for r in groups.get(gid, []):
                it = (r.get('image_type') or '').strip() or 'other'
                append_row(sku, it, r)

    for _gid, grow in groups.items():
        union: Set[str] = set()
        for r in grow:
            union.update(skus_matching_path(r.get('path') or ''))
        if len(union) != 1:
            continue
        only = next(iter(union))
        for r in grow:
            p = (r.get('path') or '').strip()
            if not p or EMPTY_IMG in p:
                continue
            it = (r.get('image_type') or '').strip() or 'other'
            if skus_matching_path(p):
                continue
            append_row(only, it, r)

    buckets: Dict[str, Dict[str, List[str]]] = {}
    for sku, by_type in raw.items():
        buckets[sku] = {}
        for itype, row_list in by_type.items():
            ordered = sorted(row_list, key=_order_key)
            urls: List[str] = []
            seen: Set[str] = set()
            for r in ordered:
                u = (r.get('path') or '').strip()
                if u and u not in seen:
                    seen.add(u)
                    urls.append(u)
            buckets[sku][itype] = urls
    return buckets


def _images_from_bucket(
    bucket: Dict[str, List[str]],
) -> Dict[str, Any]:
    attr_list = bucket.get('attr', [])
    master_list = bucket.get('master', [])
    big_list = bucket.get('big', [])
    cover = attr_list[0] if attr_list else None
    if not cover and master_list:
        cover = master_list[0]
    if not cover and big_list:
        cover = big_list[0]
    swatch = cover
    outfit_cd = list(big_list)
    outfit_cps = list(master_list)
    return {
        'cover': cover,
        'swatch': swatch,
        'outfitCd': outfit_cd,
        'outfitCps': outfit_cps,
    }


def apply_product_images(
    outfits: List[Dict[str, Any]],
    image_rows: List[Dict[str, str]],
    style_to_skus: Dict[str, List[str]],
    sku_to_id_goods: Optional[Dict[str, str]] = None,
) -> None:
    catalog_skus: Set[str] = set()
    for outfit in outfits:
        for item in outfit.get('items') or []:
            alias = (item.get('attrAlias') or '').strip()
            if alias:
                catalog_skus.add(alias)
    if not image_rows or not catalog_skus:
        return
    buckets = _build_sku_image_buckets(
        image_rows,
        catalog_skus,
        style_to_skus,
        sku_to_id_goods=sku_to_id_goods,
    )
    for outfit in outfits:
        for item in outfit.get('items') or []:
            sku = (item.get('attrAlias') or '').strip()
            if not sku or sku not in buckets:
                continue
            imgs = _images_from_bucket(buckets[sku])
            item['images'] = imgs
        items = outfit.get('items') or []
        master = next((i for i in items if i.get('isMaster')), None)
        if not master and items:
            master = items[0]
        hero = None
        if master:
            mimg = master.get('images') or {}
            ocd = mimg.get('outfitCd') or []
            if ocd:
                hero = ocd[0]
            elif mimg.get('cover'):
                hero = mimg.get('cover')
        outfit['leftHeroUrl'] = hero
        has_cd = bool(
            master
            and (master.get('images') or {}).get('outfitCd'),
        )
        outfit['flags'] = outfit.get('flags') or {}
        outfit['flags']['hasPdpOutfitImage'] = bool(has_cd)
        outfit['flags']['hasCpsOutfitImage'] = bool(
            master
            and (master.get('images') or {}).get('outfitCps'),
        )


def _row_to_item(
    row: Dict[str, str],
    *,
    is_master: bool,
    id_goods: int,
) -> Dict[str, Any]:
    sku = (row.get('货号') or '').strip()
    style = (row.get('款号') or '').strip()
    title = (row.get('商品标题') or row.get('品名') or '').strip()
    price_raw = (row.get('销售价') or '').strip()
    try:
        price = float(price_raw) if price_raw else None
    except ValueError:
        price = None
    color_name = (row.get('颜色') or '').strip()
    return {
        'attrAlias': sku,
        'isMaster': is_master,
        'idGoods': id_goods,
        'idAlias': style,
        'title': title or sku,
        'price': price,
        'color': {
            'idPa': 0,
            'colorName': color_name or '—',
        },
        'attributes': {
            'sex': (row.get('性别') or '').strip(),
            'upDown': _infer_up_down(row),
            'catType': (row.get('商品大类') or '').strip(),
            'season': (row.get('销售季节') or '').strip(),
            'series': (row.get('系列') or '').strip(),
        },
        'images': {
            'cover': None,
            'swatch': None,
            'outfitCd': [],
            'outfitCps': [],
        },
    }


def build_demo_outfits(
    by_sku: Dict[str, Dict[str, str]],
    style_to_skus: Dict[str, List[str]],
    *,
    shop_name: str,
    id_shop: int,
) -> List[Dict[str, Any]]:
    outfits: List[Dict[str, Any]] = []
    for style, skus in sorted(style_to_skus.items(), key=lambda x: x[0]):
        if not style:
            continue
        items: List[Dict[str, Any]] = []
        for idx, sku in enumerate(skus):
            row = by_sku.get(sku)
            if not row:
                continue
            gid = _stable_id_from_text(f'fila:{sku}')
            items.append(_row_to_item(row, is_master=idx == 0, id_goods=gid))
        if not items:
            continue
        master = items[0]
        name = f"{master.get('title') or style} ({style})"
        oid = _stable_id_from_text(f'fila-outfit:{style}')
        outfits.append(
            {
                'idMatch': oid,
                'name': name,
                'idShop': id_shop,
                'shopName': shop_name,
                'type': 1,
                'backgroundImg': None,
                'leftHeroUrl': None,
                'flags': {
                    'hasPdpOutfitImage': False,
                    'hasCpsOutfitImage': False,
                },
                'items': items,
            },
        )
    return outfits


def build_join_outfits(
    template_path: str,
    by_sku: Dict[str, Dict[str, str]],
    *,
    shop_substring: str,
    id_shop: int,
) -> List[Dict[str, Any]]:
    needle = shop_substring.strip().lower()
    outfits: List[Dict[str, Any]] = []
    with open(template_path, 'r', encoding='gb18030', newline='') as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            shop = (row.get('店铺') or '').strip()
            if needle not in shop.lower():
                continue
            raw_id = (row.get('ID') or '').strip().lstrip("'")
            try:
                id_match = int(raw_id)
            except ValueError:
                id_match = _stable_id_from_text(f'tpl:{raw_id}')
            master_sku = (row.get('主货号') or '').strip()
            others = _parse_template_skus(row.get('货号', ''))
            ordered = []
            if master_sku:
                ordered.append(master_sku)
            for s in others:
                if s not in ordered:
                    ordered.append(s)
            items: List[Dict[str, Any]] = []
            missing: List[str] = []
            for idx, sku in enumerate(ordered):
                prow = by_sku.get(sku)
                if not prow:
                    missing.append(sku)
                    continue
                gid = _stable_id_from_text(f'fila:{sku}')
                items.append(
                    _row_to_item(prow, is_master=idx == 0, id_goods=gid),
                )
            if missing:
                sys.stderr.write(
                    f'[skip id {raw_id}] missing SKUs in catalog: '
                    f'{", ".join(missing[:6])}'
                    f'{"..." if len(missing) > 6 else ""}\n',
                )
            if not items:
                continue
            outfits.append(
                {
                    'idMatch': id_match,
                    'name': (row.get('搭配名称') or '').strip() or str(id_match),
                    'idShop': id_shop,
                    'shopName': shop,
                    'type': 1,
                    'backgroundImg': None,
                    'leftHeroUrl': None,
                    'flags': {
                        'hasPdpOutfitImage': _parse_bool_cn(
                            row.get('是否有pdp搭配图', ''),
                        ),
                        'hasCpsOutfitImage': _parse_bool_cn(
                            row.get('是否有cps搭配图', ''),
                        ),
                    },
                    'items': items,
                },
            )
    return outfits


def main() -> int:
    from scripts._project_paths import load_paths

    paths = load_paths()
    project_root = paths['root']
    tables_dir = paths['product_dir']
    default_product = str(tables_dir / '斐乐官网商品.csv')
    default_template = str(tables_dir / '搭配模板.csv')
    default_out = str(paths['outfits_json'])
    default_images = str(paths['product_dir'] / 'product_image.csv')

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--mode',
        choices=('join', 'demo-style'),
        default='demo-style',
        help='join=按搭配模板合并；demo-style=按款号聚合展示',
    )
    parser.add_argument('--product-csv', default=default_product)
    parser.add_argument('--template-csv', default=default_template)
    parser.add_argument('--out', default=default_out)
    parser.add_argument(
        '--shop-substring',
        default='斐乐',
        help='join 模式下过滤 店铺 包含该子串（不区分大小写）',
    )
    parser.add_argument('--shop-name', default='fila小程序')
    parser.add_argument('--id-shop', type=int, default=1)
    parser.add_argument(
        '--images-csv',
        default=default_images,
        help='商品图片表（UTF-8-SIG）；不需要可传空字符串跳过',
    )
    parser.add_argument(
        '--id-goods-map',
        default='',
        help='可选 CSV：货号,id_goods，用于 path 不含货号时的图片合并',
    )
    args = parser.parse_args()

    by_sku, style_to_skus = _read_products(args.product_csv)
    if args.mode == 'demo-style':
        outfits = build_demo_outfits(
            by_sku,
            style_to_skus,
            shop_name=args.shop_name,
            id_shop=args.id_shop,
        )
    else:
        outfits = build_join_outfits(
            args.template_csv,
            by_sku,
            shop_substring=args.shop_substring,
            id_shop=args.id_shop,
        )

    image_rows: List[Dict[str, str]] = []
    img_arg = (args.images_csv or '').strip()
    if img_arg and os.path.isfile(img_arg):
        image_rows = _read_product_image_rows(img_arg)
    sku_to_id_goods: Dict[str, str] = {}
    map_arg = (args.id_goods_map or '').strip()
    if map_arg and os.path.isfile(map_arg):
        sku_to_id_goods = _read_sku_id_goods_map(map_arg)
    apply_product_images(
        outfits,
        image_rows,
        style_to_skus,
        sku_to_id_goods=sku_to_id_goods or None,
    )
    with_cover = sum(
        1
        for o in outfits
        for it in o.get('items') or []
        if (it.get('images') or {}).get('cover')
    )
    with_hero = sum(1 for o in outfits if o.get('leftHeroUrl'))

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as handle:
        json.dump(outfits, handle, ensure_ascii=False, indent=2)
        handle.write('\n')

    print(
        f'Wrote {len(outfits)} outfits to {args.out} '
        f'(mode={args.mode}, catalog SKUs={len(by_sku)}; '
        f'items with cover={with_cover}, outfits with hero={with_hero})',
    )
    if args.mode == 'join' and not outfits:
        print(
            'No outfits produced. 若搭配模板仍是迪桑特导出，请先换用斐乐搭配模板，'
            '或临时使用 --mode demo-style。',
        )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
