"""商品空图 / 占位图 URL 判定（多品牌 fishfay CDN）。"""

from __future__ import annotations

# 来自 data/tables 离线表扫描的完整占位图 URL
EMPTY_PRODUCT_IMAGE_URLS: frozenset[str] = frozenset(
    {
        "https://img.fishfay.com/theme/images/goods_empty.png",
        "https://img.fishfay.com/shopgoods/fila_empty.jpg",
        "https://img.fishfay.com/wxs_antakids/images/goods_empty.png",
        "https://img.fishfay.com/theme_arc/images/goods_empty.png",
        "https://img.fishfay.com/theme_dst/images/goods_empty.png",
        "https://img.fishfay.com/theme_kl/images/goods_empty.png",
        "https://img.fishfay.com/wxs_wilson/images/wilson_goods_empty.png",
        "https://img.fishfay.com/wxs_salomon/images/goods_empty.png",
        "https://img.fishfay.com/theme_kk/images/goods_empty.png",
        "https://img.fishfay.com/peak/images/goods_empty.jpg",
        "https://img.fishfay.com/theme_st/images/goods_empty.png",
    },
)

# 文件名片段兜底（兼容 query 参数、协议差异等）
_EMPTY_IMAGE_MARKERS: tuple[str, ...] = (
    "goods_empty.png",
    "goods_empty.jpg",
    "fila_empty.jpg",
    "wilson_goods_empty.png",
)


def is_empty_product_image_url(url: str | None) -> bool:
    """``tryon_image`` / ``index_image`` 等是否为空图（无 URL 或已知占位图 URL）。

    空串 / None 视为空图：ETL 选图阶段对无候选图的 SKU 会写入空 URL，
    这类 SKU 不应进入索引（既无法向量化也无法展示）。
    """
    text = (url or "").strip()
    if not text:
        return True
    if text in EMPTY_PRODUCT_IMAGE_URLS:
        return True
    lower = text.lower()
    return any(marker in lower for marker in _EMPTY_IMAGE_MARKERS)


def sku_has_empty_tryon_image(row: dict) -> bool:
    """SKU 的 ``tryon_image`` 是否为占位图（索引构建过滤用）。"""
    return is_empty_product_image_url(str(row.get("tryon_image") or ""))
