"""回归：coerce_llm_es_query 把 LLM 误写的扁平 match_phrase 归一为嵌套形。

复现场景：qwen 常输出 ``{"match_phrase": {"title": "粉色短裤", "boost": 8}}``，
ES 会把 ``boost`` 当成第二个字段名 → 400
``[match_phrase] query doesn't support multiple fields``。
"""

from backend.retrieval.es_intent import (
    _normalize_match_clauses,
    coerce_llm_es_query,
)


def _mp(node):
    """提取树中第一个 match_phrase 子句的 body。"""
    found = []

    def walk(n):
        if isinstance(n, dict):
            if "match_phrase" in n:
                found.append(n["match_phrase"])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return found[0] if found else None


def test_flat_match_phrase_with_boost_is_folded():
    bad = {"match_phrase": {"title": "粉色短裤", "boost": 8}}
    out = _normalize_match_clauses(bad)
    assert out == {"match_phrase": {"title": {"query": "粉色短裤", "boost": 8}}}


def test_shorthand_match_phrase_unchanged():
    """{field: str} 是合法简写，不应改动。"""
    ok = {"match_phrase": {"title": "粉色短裤"}}
    assert _normalize_match_clauses(ok) == ok


def test_nested_match_phrase_unchanged():
    ok = {"match_phrase": {"title": {"query": "粉色短裤", "boost": 8}}}
    assert _normalize_match_clauses(ok) == ok


def test_match_with_multiple_options_folded():
    bad = {"match": {"title": "粉色短裤", "operator": "and", "boost": 2}}
    out = _normalize_match_clauses(bad)
    assert out == {"match": {"title": {
        "query": "粉色短裤", "operator": "and", "boost": 2,
    }}}


def test_normalize_inside_bool_tree():
    bad = {"bool": {"must": [
        {"multi_match": {"query": "x", "fields": ["title"]}},
        {"match_phrase": {"title": "x", "boost": 8}},
    ], "filter": [{"term": {"role": "top"}}]}}
    out = _normalize_match_clauses(bad)
    assert _mp(out) == {"title": {"query": "x", "boost": 8}}
    # 其它子句不受影响
    assert out["bool"]["filter"] == [{"term": {"role": "top"}}]


def test_coerce_llm_es_query_fixes_flat_match_phrase():
    parsed = {
        "query": {
            "bool": {
                "must": [
                    {"multi_match": {"query": "粉色短裤", "fields": ["title^3"]}},
                    {"match_phrase": {"title": "粉色短裤", "boost": 8}},
                ],
                "filter": [{"term": {"role": "top"}}],
            }
        }
    }
    q = coerce_llm_es_query(parsed, "粉色短裤")
    assert _mp(q) == {"title": {"query": "粉色短裤", "boost": 8}}
