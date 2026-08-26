"""FILA hybrid 检索共享文本构建（descent build_search_text/build_semantic_text 的 fila 版）。

被 scripts/build_hybrid_index.py（Milvus BM25 源 + dense 源）与
scripts/build_fila_es_index.py（ES 富化 search_text）共用。
"""

from __future__ import annotations

from typing import Any


def _join_list(val: Any) -> str:
    if isinstance(val, list):
        return " ".join(str(x).strip() for x in val if str(x).strip())
    return str(val or "").strip()


def build_keyword_text(row: dict[str, Any]) -> str:
    """BM25 源文本：标题重复 3 次加权 + 各结构化属性 + 卖点/功能/技术/货号。

    空段自动跳过。镜像 descent data_processor.build_search_text。
    """
    title = str(row.get("title") or "").strip()
    parts: list[str] = [
        str(row.get("search_keywords") or ""),
        str(row.get("keyword") or ""),
        title, title, title,  # 标题 ×3 权重 boost
        str(row.get("product_name_short") or ""),
        str(row.get("brand_line") or ""),
        str(row.get("series") or ""),
        str(row.get("sub_series") or ""),
        str(row.get("category") or ""),
        str(row.get("category_l1") or ""),  # = cat_type
        str(row.get("up_down_raw") or ""),
        _join_list(row.get("gender")),
        str(row.get("age") or ""),
        _join_list(row.get("season")),
        str(row.get("year") or ""),
        str(row.get("modeling") or ""),
        str(row.get("length") or ""),
        str(row.get("material") or ""),  # = fabric
        str(row.get("technology") or ""),
        str(row.get("features") or ""),
        str(row.get("selling_point_label") or ""),
        str(row.get("color_name") or ""),
        str(row.get("goods_sn") or ""),
    ]
    return " ".join(p for p in parts if p).strip()


def build_semantic_text(row: dict[str, Any]) -> str:
    """dense 嵌入源：title + key:value 属性。镜像 descent build_semantic_text。"""
    parts: list[str] = [str(row.get("title") or "")]
    optional = [
        ("品牌线", row.get("brand_line")),
        ("系列", row.get("series")),
        ("小系列", row.get("sub_series")),
        ("品类", row.get("category")),
        ("大类", row.get("category_l1")),
        ("上下装", row.get("up_down_raw")),
        ("性别", _join_list(row.get("gender"))),
        ("人群", row.get("age")),
        ("季节", _join_list(row.get("season"))),
        ("年度", row.get("year")),
        ("版型", row.get("modeling")),
        ("长短", row.get("length")),
        ("面料", row.get("material")),
        ("技术", row.get("technology")),
        ("功能", row.get("features")),
        ("颜色", row.get("color_name")),
        ("卖点", row.get("selling_point_label")),
        ("款号", row.get("goods_sn")),
    ]
    for key, value in optional:
        v = str(value or "").strip()
        if v:
            parts.append(f"{key}:{v}")
    return " ".join(p for p in parts if p).strip()
