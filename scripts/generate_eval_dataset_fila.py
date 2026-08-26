"""Generate evaluation queries for FILA `fila_sku_hybrid_vectors` retrieval.

镜像参考实现 /home/jovyan/swap1/tmp/descent_product_search/scripts/generate_eval_dataset.py，
适配 fila 的字段与品类（sku_id 主键、gender/season/color_series 为数组、age∈{中大童,小童,婴幼童}、
series∈{GOLF,ATHLETICS,FUSION,...}、category_l1∈{服装,鞋类,配件}）。

数据源 data/processed/skus.jsonl 即 hybrid 索引实际写入的源数据，relevant_ids 必然命中索引。
"""

import argparse
import json
import random
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKUS_PATH = PROJECT_ROOT / 'data' / 'processed' / 'skus.jsonl'
EVAL_DIR = PROJECT_ROOT / 'data' / 'eval'
EVAL_PATH = EVAL_DIR / 'eval_queries.json'
HARD_EVAL_PATH = EVAL_DIR / 'eval_queries_hard.json'

# fila skus.jsonl 中用于构造文本的字段（对应参考 _text_of 的字段集）
_TEXT_FIELDS = [
    'title', 'product_name_short', 'category', 'category_l1', 'category_l2',
    'series', 'sub_series', 'features', 'technology', 'material',
    'selling_point_label', 'keyword', 'search_keywords', 'brand_line',
    'group_brand', 'goods_sn',
]

# fila 实际 age 取值（rewrite 抽取为 “儿童”，但库里是这三个）
_KIDS_AGES = ['中大童', '小童', '婴幼童']


def _text_of(item: dict) -> str:
    return ' '.join(
        str(v) for v in (item.get(f, '') for f in _TEXT_FIELDS) if v
    )


def _contains_any(text: str, terms: List[str]) -> bool:
    return True if not terms else any(t in text for t in terms)


def _as_list(v) -> List:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def _match_must(item: dict, key: str, want_vals: List) -> bool:
    """fila-aware must 匹配：item 字段可能是标量或数组。

    want_vals 为 “期望集合”，item 字段取值（标量或数组）与该集合交集非空即命中。
    """
    raw = item.get(key)
    item_vals = _as_list(raw)
    item_set = {str(x) for x in item_vals if x not in (None, '')}
    want_set = {str(x) for x in want_vals if x not in (None, '')}
    if not want_set:
        return True
    if not item_set:
        return False
    return bool(item_set & want_set)


def _filter_items(
    items: List[dict],
    must: Optional[Dict] = None,
    text_must_any: Optional[List[str]] = None,
    price_min: Optional[float] = None,
    price_max: Optional[float] = None,
    onsell_only: bool = True,
) -> List[dict]:
    must = must or {}
    text_must_any = text_must_any or []
    result = []
    for item in items:
        valid = True
        for key, values in must.items():
            if not _match_must(item, key, _as_list(values)):
                valid = False
                break
        if not valid:
            continue
        if onsell_only and int(item.get('onsell', 0) or 0) != 1:
            continue
        price = float(item.get('price', 0) or 0)
        if price_min is not None and price < price_min:
            continue
        if price_max is not None and (price <= 0 or price > price_max):
            continue
        if not _contains_any(_text_of(item), text_must_any):
            continue
        result.append(item)
    return result


def _make_query(
    query_id: str, query: str, category: str, items: List[dict],
    must: Optional[Dict] = None, text_must_any: Optional[List[str]] = None,
    price_min: Optional[float] = None, price_max: Optional[float] = None,
    expected_filters: Optional[Dict] = None,
) -> Optional[dict]:
    matched = _filter_items(items, must, text_must_any, price_min, price_max)
    if len(matched) < 5:
        return None
    matched = sorted(
        matched,
        key=lambda x: (str(x.get('group_brand', '')), str(x.get('series', '')), float(x.get('price', 0) or 0)),
    )
    relevant_ids = [str(item['sku_id']) for item in matched]
    high_relevant_ids = relevant_ids[:min(20, len(relevant_ids))]
    return {
        'id': query_id, 'query': query, 'category': category,
        'expected_filters': expected_filters or {},
        'relevance': {
            'must': must or {}, 'price_min': price_min, 'price_max': price_max,
            'text_must_contain_any': text_must_any or [],
        },
        'relevant_ids': relevant_ids,
        'high_relevant_ids': high_relevant_ids,
        'relevant_count': len(relevant_ids),
    }


def _build_base_groups() -> Dict[str, List[dict]]:
    groups = {
        'gender_season': [], 'kids': [], 'price': [],
        'cat_type': [], 'series': [], 'features': [],
    }

    # 性别 × 季节 × 品类词（rewrite 能抽 gender + season）
    for gender in ['男', '女']:
        for season_ch, season_word in [('春', '春季'), ('夏', '夏季'), ('秋', '秋季'), ('冬', '冬季')]:
            for term in ['外套', '羽绒服', '长裤', '卫衣', 'T恤']:
                groups['gender_season'].append({
                    'query': f'{gender}款{season_word}{term}',
                    'category': 'gender_season',
                    'must': {'gender': [gender], 'season': [season_ch]},
                    'text_must_any': [term],
                    'expected_filters': {'gender': gender, 'season': season_word},
                })

    # 儿童（age 数组用 fila 实际值；rewrite 抽 age=儿童）
    for prefix in ['儿童', '童']:
        for term in ['羽绒服', '运动鞋', '外套', '长裤']:
            groups['kids'].append({
                'query': f'{prefix}{term}',
                'category': 'kids',
                'must': {'age': _KIDS_AGES},
                'text_must_any': [term],
                'expected_filters': {'age': '儿童'},
            })

    # 价格上限（rewrite 抽 price_max）
    for threshold in [500, 1000, 1500, 2000]:
        for term in ['鞋', '外套', '卫衣']:
            groups['price'].append({
                'query': f'{threshold}元以下{term}',
                'category': 'price',
                'text_must_any': [term],
                'price_max': threshold,
                'expected_filters': {'price_max': threshold},
            })

    # 大类 category_l1（rewrite 抽 category_l1）
    for cat_l1 in ['服装', '鞋类', '配件']:
        for term in ['', '男款', '女款', '冬季']:
            groups['cat_type'].append({
                'query': f'{term} {cat_l1}'.strip(),
                'category': 'cat_type',
                'must': {'category_l1': cat_l1},
                'text_must_any': [cat_l1] if not term else [cat_l1, term],
                'expected_filters': {'category_l1': cat_l1},
            })

    # 系列（纯文本维度，无 filter；测排序能否召回 series 文本）
    for series in ['GOLF', 'ATHLETICS', 'FUSION', 'HERITAGE', 'TENNIS']:
        for term in ['外套', '长裤', '鞋']:
            groups['series'].append({
                'query': f'{series} {term}',
                'category': 'series',
                'text_must_any': [series, term],
                'expected_filters': {},
            })

    # 功能（纯文本维度）
    for feature in ['防水', '速干', '防晒', '保暖']:
        groups['features'].append({
            'query': f'{feature}外套',
            'category': 'features',
            'text_must_any': [feature, '外套'],
            'expected_filters': {},
        })

    return groups


def _build_hard_groups() -> Dict[str, List[dict]]:
    groups = {
        'colloquial': [], 'typo': [], 'mixed_constraints': [],
        'long_tail_intent': [],
    }

    def add(name, query, must, terms, expected, price_max=None):
        groups[name].append({
            'query': query, 'category': name, 'must': must,
            'text_must_any': terms, 'expected_filters': expected,
            'price_max': price_max,
        })

    # 口语化
    add('colloquial', '给老公冬天穿的羽绒服', {'season': ['冬']}, ['羽绒服', '男'], {'season': '冬季'})
    add('colloquial', '女生通勤保暖的外套', {'gender': ['女']}, ['外套', '保暖'], {'gender': '女'})
    add('colloquial', '小朋友上学的鞋子', {'age': _KIDS_AGES}, ['鞋'], {'age': '儿童'})
    add('colloquial', '预算一千五买男鞋', {'gender': ['男']}, ['鞋'], {'gender': '男', 'price_max': 1500}, 1500)
    add('colloquial', '下雨天能穿的外套', {}, ['防水', '外套'], {})
    add('colloquial', '高尔夫运动的polo衫', {}, ['GOLF', 'POLO'], {})

    # 错字
    add('typo', '男款东季羽绒服', {'gender': ['男'], 'season': ['冬']}, ['羽绒服'], {'gender': '男', 'season': '冬季'})
    add('typo', '女童运动鞵', {'age': _KIDS_AGES}, ['鞋'], {'age': '儿童'})
    add('typo', '防水外塔', {}, ['防水', '外套'], {})
    add('typo', '斐乐运动鞋', {}, ['FILA', '鞋'], {})

    # 混合约束
    add('mixed_constraints', '女款 冬季 羽绒服 2000以内', {'gender': ['女'], 'season': ['冬']}, ['羽绒服'], {'gender': '女', 'season': '冬季', 'price_max': 2000}, 2000)
    add('mixed_constraints', '男 上装 卫衣 1000以下', {'gender': ['男'], 'up_down_raw': ['上装']}, ['卫衣'], {'gender': '男', 'up_down': '上装'}, 1000)
    add('mixed_constraints', '儿童 冬季 羽绒服 1000以内', {'age': _KIDS_AGES, 'season': ['冬']}, ['羽绒服'], {'age': '儿童', 'season': '冬季', 'price_max': 1000}, 1000)
    add('mixed_constraints', '鞋类 男款 500以下', {'category_l1': '鞋类', 'gender': ['男']}, ['鞋'], {'category_l1': '鞋类', 'gender': '男'}, 500)

    # 长尾意图
    add('long_tail_intent', '冬天保暖的专业羽绒服', {'season': ['冬']}, ['羽绒服', '保暖'], {})
    add('long_tail_intent', '春秋天跑步穿的衣服', {'season': ['春', '秋']}, ['跑步', '运动', '服'], {})
    add('long_tail_intent', '适合打高尔夫的长裤', {}, ['GOLF', '长裤', '裤'], {})
    add('long_tail_intent', '防晒速干的夏季T恤', {'season': ['夏']}, ['防晒', '速干', 'T恤'], {})
    add('long_tail_intent', '明星同款的外套', {}, ['同款', '外套'], {})

    return groups


def _select_queries(items, groups, quota, target):
    eval_queries = []
    used = set()
    qid = 1
    for category in groups:
        for candidate in groups[category]:
            if len(eval_queries) >= target:
                break
            if sum(1 for x in eval_queries if x['category'] == category) >= quota.get(category, 0):
                break
            if candidate['query'] in used:
                continue
            item = _make_query(
                f'Q{qid:03d}', candidate['query'], category, items,
                must=candidate.get('must'), text_must_any=candidate.get('text_must_any'),
                price_min=candidate.get('price_min'), price_max=candidate.get('price_max'),
                expected_filters=candidate.get('expected_filters'),
            )
            if item:
                eval_queries.append(item)
                used.add(candidate['query'])
                qid += 1
    # Fill remaining
    all_candidates = []
    for category in groups:
        all_candidates.extend(groups[category])
    for candidate in all_candidates:
        if len(eval_queries) >= target:
            break
        if candidate['query'] in used:
            continue
        item = _make_query(
            f'Q{qid:03d}', candidate['query'], candidate['category'], items,
            must=candidate.get('must'), text_must_any=candidate.get('text_must_any'),
            price_min=candidate.get('price_min'), price_max=candidate.get('price_max'),
            expected_filters=candidate.get('expected_filters'),
        )
        if item:
            eval_queries.append(item)
            used.add(candidate['query'])
            qid += 1
    return eval_queries


def _load_items() -> List[dict]:
    items: List[dict] = []
    with SKUS_PATH.open('r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items


def main():
    parser = argparse.ArgumentParser(description='Generate eval queries for FILA hybrid search')
    parser.add_argument('--variant', default='base', choices=['base', 'hard'])
    parser.add_argument('--output', default='')
    args = parser.parse_args()

    random.seed(42)
    items = _load_items()
    print(f'loaded skus={len(items)} from {SKUS_PATH}')

    if args.variant == 'hard':
        groups = _build_hard_groups()
        quota = {'colloquial': 8, 'typo': 6, 'mixed_constraints': 8, 'long_tail_intent': 8}
        target = 30
        default_output = HARD_EVAL_PATH
    else:
        groups = _build_base_groups()
        quota = {
            'gender_season': 12, 'kids': 8, 'price': 6,
            'cat_type': 8, 'series': 8, 'features': 4,
        }
        target = 50
        default_output = EVAL_PATH

    eval_queries = _select_queries(items, groups, quota, target=target)
    output_path = Path(args.output) if args.output else default_output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open('w', encoding='utf-8') as f:
        json.dump(eval_queries, f, ensure_ascii=False, indent=2)

    # per-category sanity
    cat_counts: Dict[str, int] = {}
    cat_rel: Dict[str, List[int]] = {}
    for q in eval_queries:
        cat_counts[q['category']] = cat_counts.get(q['category'], 0) + 1
        cat_rel.setdefault(q['category'], []).append(q['relevant_count'])
    print(f'generated_queries={len(eval_queries)} variant={args.variant} path={output_path}')
    for cat in sorted(cat_counts):
        rels = cat_rel[cat]
        print(f'  {cat:20s} n={cat_counts[cat]:2d} '
              f'avg_rel={sum(rels)/len(rels):.1f} min={min(rels)} max={max(rels)}')
    if eval_queries:
        print('sample:', eval_queries[0]['id'], eval_queries[0]['query'], eval_queries[0]['relevant_count'])


if __name__ == '__main__':
    main()
