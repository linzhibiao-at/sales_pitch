"""FILA Milvus hybrid 检索（descent search_engine 的 fila 版）。

单集合 fila_sku_hybrid_vectors：sparse_vector(BM25) + dense_vector(COSINE)，
MilvusClient.hybrid_search(AnnSearchRequest ×2 + RRFRanker/WeightedRanker)。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from backend.config import (
    get_milvus_token,
    get_milvus_uri,
    is_milvus_lite_local_uri,
    load_config,
    restore_stashed_milvus_uri,
    stash_milvus_db_uri_before_pymilvus_import,
)
from backend.embedding_client import embed_text

logger = logging.getLogger(__name__)

# pymilvus import 期间避免解析到本地 *.db（与 backend/retrieval/milvus_client.py 同策略）
_cfg0 = load_config()
_mv0 = _cfg0.get("milvus") or {}
_uri_env0 = str(_mv0.get("uri_env") or "FILA_MILVUS_URI")
stash_milvus_db_uri_before_pymilvus_import(_uri_env0)
try:
    from pymilvus import AnnSearchRequest, MilvusClient, RRFRanker, WeightedRanker
except ImportError:  # pragma: no cover
    AnnSearchRequest = None  # type: ignore
    MilvusClient = None  # type: ignore
    RRFRanker = None  # type: ignore
    WeightedRanker = None  # type: ignore
finally:
    restore_stashed_milvus_uri()

try:
    import jieba  # type: ignore
    _HAS_JIEBA = True
except ImportError:
    _HAS_JIEBA = False

DEFAULT_OUTPUT_FIELDS = [
    "sku_id", "title", "product_name_short", "goods_sn", "brand_line",
    "category", "category_l1", "category_l2", "up_down_raw", "role",
    "color_name", "color_series", "gender", "season", "series", "sub_series",
    "year", "modeling", "length", "length_class", "layer", "coverage",
    "is_intimate", "scene_domain", "group_brand", "technology", "features",
    "selling_point_label", "material", "age", "price", "market_price",
    "onsell", "sales", "up_time", "id_goods", "sku_count",
]

_ATTR_WORDS: dict[str, dict[str, str]] = {
    "男士": {"gender": "男"}, "男款": {"gender": "男"}, "男子": {"gender": "男"},
    "女士": {"gender": "女"}, "女款": {"gender": "女"}, "女子": {"gender": "女"},
    "中性": {"gender": "中性"},
    "男童": {"gender": "男童"}, "女童": {"gender": "女童"},
    "儿童": {"age": "儿童"}, "童装": {"age": "儿童"}, "童鞋": {"age": "儿童"},
    "春季": {"season": "春季"}, "夏季": {"season": "夏季"},
    "秋季": {"season": "秋季"}, "冬季": {"season": "冬季"}, "冬天": {"season": "冬季"},
    "上装": {"up_down": "上装"}, "下装": {"up_down": "下装"},
}

_STOPWORDS = {
    "的", "了", "吗", "呢", "啊", "吧", "呀", "适合", "推荐", "有没有", "有什么",
    "一双", "一件", "一条", "一个", "一款", "买", "送", "穿", "给", "想", "要",
    "找", "看看", "能", "可以", "比较", "最", "很", "好", "什么", "哪个", "哪种",
    "我", "你", "他", "她", "它",
}

_PRICE_RE = re.compile(r"(\d+)\s*(?:元|块)?(?:以[内下]|以下)")
_PRICE_RANGE_RE = re.compile(r"(\d+)\s*[-~到至]\s*(\d+)\s*(?:元|块)?")
_WS_RE = re.compile(r"\s+")


@dataclass
class RewriteResult:
    keyword_query: str
    semantic_query: str
    filters: dict = field(default_factory=dict)
    source: str = "rule"


def rule_rewrite(query: str, existing_filters: Optional[dict] = None) -> RewriteResult:
    """规则改写：价格正则 + 属性词抽取 + 停用词过滤。

    jieba 可用时用其分词；否则退化为子串属性抽取 + 空白拆分（保留价格/属性能力）。
    """
    filters = dict(existing_filters) if existing_filters else {}
    text = query
    m = _PRICE_RE.search(text)
    if m:
        filters.setdefault("price_max", int(m.group(1)))
        text = text[: m.start()] + text[m.end():]
    else:
        rm = _PRICE_RANGE_RE.search(text)
        if rm:
            filters.setdefault("price_min", int(rm.group(1)))
            filters.setdefault("price_max", int(rm.group(2)))
            text = text[: rm.start()] + text[rm.end():]

    # 属性词子串抽取（长词优先，避免短词误匹配）
    work = text
    for word in sorted(_ATTR_WORDS, key=len, reverse=True):
        if word in work:
            for k, v in _ATTR_WORDS[word].items():
                filters.setdefault(k, v)
            work = work.replace(word, " ")

    if _HAS_JIEBA:
        parts: list[str] = []
        for tok in jieba.cut(work):
            tok = tok.strip()
            if not tok or tok in _STOPWORDS:
                continue
            parts.append(tok)
        kw = "".join(parts).strip()
    else:
        toks = [t for t in _WS_RE.split(work) if t]
        parts = [t for t in toks if t not in _STOPWORDS]
        kw = "".join(parts).strip()

    kw = kw or query
    return RewriteResult(
        keyword_query=kw,
        semantic_query=query,
        filters=filters,
        source="rule" if _HAS_JIEBA else "rule_nojieba",
    )


def llm_rewrite(query: str) -> Optional[RewriteResult]:
    """可选 LLM 改写（默认关）。fila 关键词来自 intent 层，接入时 skip_rewrite。"""
    cfg = load_config()
    if not (cfg.get("hybrid") or {}).get("llm_rewrite"):
        return None
    return None  # 预留：LLM 改写未启用


def rewrite_query(query: str, existing_filters: Optional[dict] = None) -> RewriteResult:
    result = llm_rewrite(query) or rule_rewrite(query, existing_filters)
    if existing_filters:
        for k, v in existing_filters.items():
            result.filters.setdefault(k, v)
    return result


def build_filter_expr(filters: Optional[dict] = None) -> str:
    """fila 标量过滤 expr（对应 descent build_filter_expr 的 fila 子集）。"""
    if not filters:
        return ""
    conds: list[str] = []
    if "price_min" in filters:
        conds.append(f"price >= {filters['price_min']}")
    if "price_max" in filters:
        conds.append(f"price <= {filters['price_max']}")
    if "gender" in filters:
        conds.append(f'gender == "{filters["gender"]}"')
    if "age" in filters:
        conds.append(f'age == "{filters["age"]}"')
    if "season" in filters:
        conds.append(f'season like "%{filters["season"]}%"')
    if "brand_line" in filters:
        conds.append(f'brand_line == "{filters["brand_line"]}"')
    if "category_l1" in filters:
        conds.append(f'category_l1 == "{filters["category_l1"]}"')
    if "up_down" in filters:
        conds.append(f'up_down_raw == "{filters["up_down"]}"')
    if "onsell" in filters:
        conds.append(f"onsell == {filters['onsell']}")
    return " and ".join(conds)


def format_results(raw_results: Any, output_fields: list[str]) -> list[dict]:
    if not raw_results:
        return []
    hits = raw_results[0] if isinstance(raw_results, list) and raw_results else raw_results
    out: list[dict] = []
    for hit in hits:
        entity = hit.get("entity", {}) if isinstance(hit, dict) else getattr(hit, "entity", {})
        score = hit.get("distance", 0) if isinstance(hit, dict) else getattr(hit, "distance", 0)
        item_id = hit.get("id", "") if isinstance(hit, dict) else getattr(hit, "id", "")
        row = entity if isinstance(entity, dict) else {}
        item: dict[str, Any] = {
            "sku_id": str(row.get("sku_id") or item_id or ""),
            "score": round(float(score), 4),
        }
        for f in output_fields:
            if f != "sku_id":
                item[f] = row.get(f, "")
        out.append(item)
    return out


class FilaSkuHybridSearcher:
    def __init__(self, client: Any = None) -> None:
        self._client = client
        cfg = load_config()
        mv = cfg.get("milvus") or {}
        self.collection_name = str(
            (mv.get("collections") or {}).get("sku_hybrid_vectors") or "fila_sku_hybrid_vectors"
        )
        hyb = cfg.get("hybrid") or {}
        self._kw_w = float(hyb.get("keyword_weight", 0.2))
        self._sem_w = float(hyb.get("semantic_weight", 0.8))
        self._ranker = str(hyb.get("ranker", "rrf"))
        self._limit = int(hyb.get("default_limit", 20))
        self._nprobe = int(hyb.get("nprobe", 16))

    @property
    def client(self) -> Any:
        if self._client is None:
            cfg = load_config()
            uri = get_milvus_uri(cfg)
            if not uri:
                raise RuntimeError("MILVUS_URI 为空，请配置 milvus")
            if is_milvus_lite_local_uri(uri):
                raise RuntimeError("hybrid search 不支持 local Milvus Lite，请用 cloud uri")
            self._client = MilvusClient(uri=uri, token=get_milvus_token(cfg) or None)
        return self._client

    def _encode(self, query: str) -> list[float]:
        vec = embed_text(query)
        if not vec:
            raise RuntimeError("embed_text 返回空，无法做 semantic/hybrid 检索")
        return vec

    def search_keyword(
        self, query: str, *, expr: Optional[str] = None, limit: Optional[int] = None,
        output_fields: Optional[list[str]] = None, skip_rewrite: bool = False,
    ) -> list[dict]:
        rw = RewriteResult(query, query, {}, "passthrough") if skip_rewrite else rewrite_query(query)
        res = self.client.search(
            collection_name=self.collection_name,
            data=[rw.keyword_query],
            anns_field="sparse_vector",
            search_params={"metric_type": "BM25"},
            limit=limit or self._limit,
            output_fields=output_fields or DEFAULT_OUTPUT_FIELDS,
            filter=expr or None,
        )
        return format_results(res, output_fields or DEFAULT_OUTPUT_FIELDS)

    def search_semantic(
        self, query: str, *, expr: Optional[str] = None, limit: Optional[int] = None,
        output_fields: Optional[list[str]] = None, skip_rewrite: bool = False,
    ) -> list[dict]:
        rw = RewriteResult(query, query, {}, "passthrough") if skip_rewrite else rewrite_query(query)
        vec = self._encode(rw.semantic_query)
        res = self.client.search(
            collection_name=self.collection_name,
            data=[vec],
            anns_field="dense_vector",
            search_params={"metric_type": "COSINE", "params": {"nprobe": self._nprobe}},
            limit=limit or self._limit,
            output_fields=output_fields or DEFAULT_OUTPUT_FIELDS,
            filter=expr or None,
        )
        return format_results(res, output_fields or DEFAULT_OUTPUT_FIELDS)

    def search_hybrid(
        self, query: str, *, expr: Optional[str] = None, limit: Optional[int] = None,
        kw_w: Optional[float] = None, sem_w: Optional[float] = None,
        ranker: Optional[str] = None, output_fields: Optional[list[str]] = None,
        skip_rewrite: bool = True,
    ) -> list[dict]:
        rw = RewriteResult(query, query, {}, "passthrough") if skip_rewrite else rewrite_query(query)
        vec = self._encode(rw.semantic_query)
        fetch_limit = limit or self._limit
        keyword_req = AnnSearchRequest(
            data=[rw.keyword_query], anns_field="sparse_vector",
            param={"metric_type": "BM25"}, limit=fetch_limit, expr=expr or None,
        )
        semantic_req = AnnSearchRequest(
            data=[vec], anns_field="dense_vector",
            param={"metric_type": "COSINE", "params": {"nprobe": self._nprobe}},
            limit=fetch_limit, expr=expr or None,
        )
        use_ranker = ranker or self._ranker
        if use_ranker == "rrf":
            rerank = RRFRanker(k=60)
        else:
            rerank = WeightedRanker(
                kw_w if kw_w is not None else self._kw_w,
                sem_w if sem_w is not None else self._sem_w,
            )
        res = self.client.hybrid_search(
            collection_name=self.collection_name,
            reqs=[keyword_req, semantic_req],
            ranker=rerank,
            limit=fetch_limit,
            output_fields=output_fields or DEFAULT_OUTPUT_FIELDS,
        )
        return format_results(res, output_fields or DEFAULT_OUTPUT_FIELDS)

    def get_skus_by_ids(self, sku_ids: list[str], output_fields: Optional[list[str]] = None) -> list[dict]:
        if not sku_ids:
            return []
        in_list = ", ".join(f'"{s}"' for s in sku_ids)
        res = self.client.query(
            collection_name=self.collection_name,
            filter=f"sku_id in [{in_list}]",
            output_fields=output_fields or DEFAULT_OUTPUT_FIELDS,
        )
        id_map = {it.get("sku_id", ""): it for it in res}
        return [id_map[s] for s in sku_ids if s in id_map]
