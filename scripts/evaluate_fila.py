"""
Evaluate FILA `fila_sku_hybrid_vectors` retrieval quality.

镜像参考实现 /home/jovyan/swap1/tmp/descent_product_search/scripts/evaluate.py，
指标：NDCG@10 / Precision@5,10 / Recall@10 / MRR / Hit@5 / MAP@10 + filter_accuracy + 每类目汇总。
检索入口复用 backend.retrieval.hybrid_search.FilaSkuHybridSearcher（keyword/semantic/hybrid）。
命中按 sku_id 比对（high_relevant=3 / relevant=2 / else 0）。

注意：fila 的 build_filter_expr 对 gender/season(ARRAY) 发 == / like 会失效、age 抽 “儿童”
与库值(中大童/小童/婴幼童)不一致，故 eval 自带 build_eval_filter_expr 做 fila-correct 翻译，
不改 backend 检索代码。
"""

import argparse
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

EVAL_DIR = PROJECT_ROOT / 'data' / 'eval'
EVAL_QUERIES_PATH = EVAL_DIR / 'eval_queries.json'
HARD_EVAL_QUERIES_PATH = EVAL_DIR / 'eval_queries_hard.json'
EVAL_RESULTS_DIR = EVAL_DIR / 'eval_results'

# fila age 实际值（rewrite 抽 age=儿童 时映射到这里）
_KIDS_AGES = ['中大童', '小童', '婴幼童']


def dcg_at_k(relevances: list, k: int) -> float:
    return sum(rel / math.log2(idx + 2) for idx, rel in enumerate(relevances[:k]))


def ndcg_at_k(relevances: list, k: int) -> float:
    dcg = dcg_at_k(relevances, k)
    ideal = dcg_at_k(sorted(relevances, reverse=True), k)
    return dcg / ideal if ideal > 0 else 0.0


def precision_at_k(relevances: list, k: int) -> float:
    rels = relevances[:k]
    if not rels:
        return 0.0
    return sum(1 for rel in rels if rel > 0) / len(rels)


def recall_at_k(relevances: list, relevant_total: int, k: int) -> float:
    if relevant_total <= 0:
        return 0.0
    return sum(1 for rel in relevances[:k] if rel > 0) / relevant_total


def mrr_score(relevances: list) -> float:
    for idx, rel in enumerate(relevances):
        if rel > 0:
            return 1.0 / (idx + 1)
    return 0.0


def hit_rate_at_k(relevances: list, k: int) -> float:
    return 1.0 if any(rel > 0 for rel in relevances[:k]) else 0.0


def map_at_k(relevances: list, k: int) -> float:
    hit_count = 0
    ap_sum = 0.0
    for idx, rel in enumerate(relevances[:k], start=1):
        if rel > 0:
            hit_count += 1
            ap_sum += hit_count / idx
    if hit_count == 0:
        return 0.0
    return ap_sum / hit_count


def evaluate_filter_accuracy(expected: dict, actual: dict) -> dict:
    expected_items = {f'{key}={value}' for key, value in expected.items()}
    actual_items = {f'{key}={value}' for key, value in actual.items()}
    true_positive = len(expected_items & actual_items)
    precision = true_positive / len(actual_items) if actual_items else (
        1.0 if not expected_items else 0.0
    )
    recall = true_positive / len(expected_items) if expected_items else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        'precision': round(precision, 4),
        'recall': round(recall, 4),
        'f1': round(f1, 4),
    }


def build_relevance_list(query_cfg: dict, search_results: list) -> list:
    relevant_ids = set(query_cfg.get('relevant_ids', []))
    high_relevant_ids = set(query_cfg.get('high_relevant_ids', []))
    relevances = []
    for item in search_results:
        sku_id = str(item.get('sku_id', ''))
        if sku_id in high_relevant_ids:
            relevances.append(3)
        elif sku_id in relevant_ids:
            relevances.append(2)
        else:
            relevances.append(0)
    return relevances


def _milvus_quote(v: str) -> str:
    return '"' + str(v).replace('\\', '\\\\').replace('"', '\\"') + '"'


def build_eval_filter_expr(filters: Optional[dict] = None, *, onsell_only: bool = True) -> str:
    """fila-correct Milvus expr：把 rewrite 抽出的 filters 翻译成对 fila schema 合法的谓词。

    与 backend 的 build_filter_expr 区别：gender/season 用 array_contains_any（数组字段），
    age=儿童 映射到库实际值，season 春季→春 取首字。默认并 onsell==1。
    """
    if not filters:
        filters = {}
    conds: list = []
    if 'price_min' in filters and filters['price_min'] is not None:
        conds.append(f"price >= {filters['price_min']}")
    if 'price_max' in filters and filters['price_max'] is not None:
        conds.append(f"price <= {filters['price_max']}")
    if 'gender' in filters and filters['gender']:
        conds.append(f'array_contains_any(gender, [{_milvus_quote(filters["gender"])}])')
    if 'season' in filters and filters['season']:
        # season 是 VARCHAR（存 ",".join(["秋"])），用 like 子串；rewrite 出 春季/夏季… 取首字
        ch = str(filters['season'])[0:1]
        ch = ch.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        conds.append(f'season like "%{ch}%"')
    if 'age' in filters and filters['age']:
        # rewrite 出 儿童 → 库里 中大童/小童/婴幼童
        ages = _KIDS_AGES if str(filters['age']) == '儿童' else [filters['age']]
        conds.append('age in [' + ', '.join(_milvus_quote(a) for a in ages) + ']')
    if 'up_down' in filters and filters['up_down']:
        conds.append(f'up_down_raw == {_milvus_quote(filters["up_down"])}')
    if 'category_l1' in filters and filters['category_l1']:
        conds.append(f'category_l1 == {_milvus_quote(filters["category_l1"])}')
    if 'brand_line' in filters and filters['brand_line']:
        conds.append(f'brand_line == {_milvus_quote(filters["brand_line"])}')
    if onsell_only:
        conds.append('onsell == 1')
    return ' and '.join(conds)


def compute_summary(results: dict) -> dict:
    summary = {}
    modes = results['modes']
    queries = results['queries']
    metric_names = [
        'ndcg_10', 'precision_5', 'precision_10', 'recall_10',
        'mrr', 'hit_rate_5', 'map_10', 'result_count',
    ]
    for mode in modes:
        mode_summary = {}
        for metric_name in metric_names:
            values = [entry['modes'][mode][metric_name] for entry in queries]
            mode_summary[metric_name] = round(sum(values) / len(values), 4) if values else 0.0
        mode_summary['zero_result_rate'] = round(
            sum(1 for entry in queries if entry['modes'][mode]['result_count'] == 0) / len(queries),
            4,
        ) if queries else 0.0
        summary[mode] = mode_summary

    filter_values = [entry['rewrite']['filter_accuracy'] for entry in queries]
    if filter_values:
        summary['filter_accuracy'] = {
            'precision': round(sum(item['precision'] for item in filter_values) / len(filter_values), 4),
            'recall': round(sum(item['recall'] for item in filter_values) / len(filter_values), 4),
            'f1': round(sum(item['f1'] for item in filter_values) / len(filter_values), 4),
        }
    else:
        summary['filter_accuracy'] = {'precision': 0.0, 'recall': 0.0, 'f1': 0.0}

    category_metrics: dict = {}
    for entry in queries:
        cat = entry.get('category', 'unknown')
        if cat not in category_metrics:
            category_metrics[cat] = {m: [] for m in metric_names}
        for mode in modes:
            for m in metric_names:
                category_metrics[cat][m].append(entry['modes'][mode][m])
    summary['by_category'] = {}
    for cat, mvals in sorted(category_metrics.items()):
        cat_summary = {}
        for m in metric_names:
            vals = mvals[m]
            cat_summary[m] = round(sum(vals) / len(vals), 4) if vals else 0.0
        cat_summary['count'] = len(mvals['ndcg_10'])
        summary['by_category'][cat] = cat_summary

    return summary


def print_report(results: dict) -> None:
    print('\n' + '=' * 72)
    print('FILA fila_sku_hybrid_vectors 检索评测报告')
    print(f"time={results['timestamp']} queries={results['query_count']} modes={results['modes']}")
    print('=' * 72)

    fa = results['summary']['filter_accuracy']
    print(f"Filter准确率: P={fa['precision']:.4f} R={fa['recall']:.4f} F1={fa['f1']:.4f}")

    for mode in results['modes']:
        metric = results['summary'][mode]
        print(
            f"[{mode}] "
            f"NDCG@10={metric['ndcg_10']:.4f} "
            f"P@5={metric['precision_5']:.4f} "
            f"P@10={metric['precision_10']:.4f} "
            f"R@10={metric['recall_10']:.4f} "
            f"MRR={metric['mrr']:.4f} "
            f"Hit@5={metric['hit_rate_5']:.4f} "
            f"MAP@10={metric['map_10']:.4f} "
            f"AvgCnt={metric['result_count']:.2f} "
            f"ZeroRate={metric['zero_result_rate']:.2%}"
        )

    by_cat = results['summary'].get('by_category', {})
    if by_cat:
        focus_mode = 'hybrid' if 'hybrid' in results['modes'] else results['modes'][-1]
        print(f'\n--- Per-Category ({focus_mode}) ---')
        for cat, vals in by_cat.items():
            print(
                f"  {cat:20s} n={vals['count']:2d} "
                f"NDCG@10={vals['ndcg_10']:.4f} "
                f"P@5={vals['precision_5']:.4f} "
                f"R@10={vals['recall_10']:.4f} "
                f"MRR={vals['mrr']:.4f}"
            )


def save_results(results: dict) -> Path:
    EVAL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_path = EVAL_RESULTS_DIR / f'eval_{ts}.json'
    with output_path.open('w', encoding='utf-8') as file_obj:
        json.dump(results, file_obj, ensure_ascii=False, indent=2)
    return output_path


def _search_mode(searcher, mode: str, query: str, expr: str, limit: int):
    """统一调用三模式，强制 skip_rewrite=False 让 searcher 内部走 rewrite。"""
    if mode == 'keyword':
        return searcher.search_keyword(query, expr=expr, limit=limit, skip_rewrite=False)
    if mode == 'semantic':
        return searcher.search_semantic(query, expr=expr, limit=limit, skip_rewrite=False)
    if mode == 'hybrid':
        return searcher.search_hybrid(query, expr=expr, limit=limit, skip_rewrite=False)
    raise ValueError(f'unknown mode: {mode}')


def run_evaluation(eval_queries: list, modes: list, limit: int) -> dict:
    from backend.retrieval.hybrid_search import FilaSkuHybridSearcher, rewrite_query

    searcher = FilaSkuHybridSearcher()
    result = {
        'timestamp': datetime.now().isoformat(timespec='seconds'),
        'query_count': len(eval_queries),
        'modes': modes,
        'queries': [],
        'summary': {},
    }

    total = len(eval_queries)
    for idx, query_cfg in enumerate(eval_queries, start=1):
        query = query_cfg['query']
        print(f'[{idx}/{total}] {query} ...', end='', flush=True)

        t_rewrite_begin = time.time()
        rewrite_result = rewrite_query(query)
        rewrite_ms = round((time.time() - t_rewrite_begin) * 1000)
        filter_acc = evaluate_filter_accuracy(
            query_cfg.get('expected_filters', {}),
            rewrite_result.filters,
        )
        expr = build_eval_filter_expr(rewrite_result.filters)

        row = {
            'id': query_cfg['id'],
            'query': query,
            'category': query_cfg['category'],
            'rewrite': {
                'source': rewrite_result.source,
                'keyword_query': rewrite_result.keyword_query,
                'semantic_query': rewrite_result.semantic_query,
                'filters': rewrite_result.filters,
                'filter_accuracy': filter_acc,
                'expr': expr,
            },
            'modes': {},
            'timing_ms': {'rewrite': rewrite_ms},
        }

        relevant_total = int(query_cfg.get('relevant_count', len(query_cfg.get('relevant_ids', []))))

        for mode in modes:
            t_search_begin = time.time()
            try:
                search_results = _search_mode(searcher, mode, query, expr, limit)
                err = None
            except Exception as exc:  # 单 query 异常不中断整跑
                search_results = []
                err = f'{type(exc).__name__}: {exc}'
            search_ms = round((time.time() - t_search_begin) * 1000)
            relevances = build_relevance_list(query_cfg, search_results)

            metrics = {
                'ndcg_10': round(ndcg_at_k(relevances, 10), 4),
                'precision_5': round(precision_at_k(relevances, 5), 4),
                'precision_10': round(precision_at_k(relevances, 10), 4),
                'recall_10': round(recall_at_k(relevances, relevant_total, 10), 4),
                'mrr': round(mrr_score(relevances), 4),
                'hit_rate_5': round(hit_rate_at_k(relevances, 5), 4),
                'map_10': round(map_at_k(relevances, 10), 4),
                'result_count': len(search_results),
                'relevances_top10': relevances[:10],
            }
            if err:
                metrics['error'] = err
            row['modes'][mode] = metrics
            row['timing_ms'][mode] = search_ms

        result['queries'].append(row)
        focus_mode = 'hybrid' if 'hybrid' in modes else modes[-1]
        print(
            f" done {focus_mode}:"
            f"NDCG@10={row['modes'][focus_mode]['ndcg_10']:.4f} "
            f"P@5={row['modes'][focus_mode]['precision_5']:.4f}"
        )

    result['summary'] = compute_summary(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description='Evaluate FILA hybrid search quality')
    parser.add_argument(
        '--mode', default='all',
        choices=['keyword', 'semantic', 'hybrid', 'all'],
        help='Which search mode to evaluate',
    )
    parser.add_argument(
        '--variant', default='base', choices=['base', 'hard'],
        help='Which eval query set to use',
    )
    parser.add_argument(
        '--queries', default='',
        help='Evaluation query json path (overrides --variant)',
    )
    parser.add_argument('--limit', type=int, default=20, help='top-k per query')
    args = parser.parse_args()

    if args.queries:
        queries_path = Path(args.queries)
    else:
        queries_path = HARD_EVAL_QUERIES_PATH if args.variant == 'hard' else EVAL_QUERIES_PATH
    if not queries_path.exists():
        raise FileNotFoundError(
            f'{queries_path} not found, run scripts/generate_eval_dataset_fila.py first'
        )
    with queries_path.open('r', encoding='utf-8') as file_obj:
        eval_queries = json.load(file_obj)

    modes = ['keyword', 'semantic', 'hybrid'] if args.mode == 'all' else [args.mode]
    print(f'load eval queries={len(eval_queries)} path={queries_path} modes={modes} limit={args.limit}')

    started_at = time.time()
    results = run_evaluation(eval_queries, modes=modes, limit=args.limit)
    print_report(results)
    output_path = save_results(results)
    elapsed = time.time() - started_at
    print(f'\nSaved: {output_path}')
    print(f'Total elapsed: {elapsed:.1f}s ({elapsed / max(len(eval_queries), 1):.2f}s/query)')


if __name__ == '__main__':
    main()
