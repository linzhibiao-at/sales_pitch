"""对外推荐接口：图片抓取 + 出参 reshape（按 docs/FILA穿搭推荐入参出参.md）。

仅做入参映射与出参整形，底层推荐引擎复用 ``RecommendService.chat_stream``。
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from backend.services.card_builder import _item_sku_id

logger = logging.getLogger(__name__)

# 虚拟图锚点 sku_id 前缀（见 recommend_service.build_upload_anchor_row）
_IMG_ANCHOR_PREFIX = "img_"

# 上传图被写入 item 后的 data URI 前缀，用于识别“被上传图覆盖的图”
_DATA_URI_PREFIX = "data:image"


def fetch_image_url_to_base64(url: str) -> str | None:
    """抓取 image_url 转 base64；失败返回 None（降级为仅用 input_sku_id 锚点）。"""
    url = (url or "").strip()
    if not url:
        return None
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            ctype = (resp.headers.get("content-type") or "").lower()
            if not ctype.startswith("image/"):
                logger.warning(
                    "[对外接口·抓图] url=%s content-type=%s 非图片，跳过",
                    url[:120], ctype,
                )
                return None
            return base64.b64encode(resp.content).decode("ascii")
    except Exception as exc:
        logger.warning("[对外接口·抓图] url=%s 抓取失败: %s", url[:120], exc)
        return None


def _resolve_sku_image_url(
    item: dict[str, Any],
    row: dict[str, Any] | None,
    image_url: str | None,
) -> str:
    """解析 item 的 sku_image_url。

    - 虚拟图锚点（sku_id 以 img_ 开头）：返回入参 image_url（无则 ""）。
    - 真实 SKU item：取 item 的 tryon_image；若为 data:image（被上传图覆盖）
      则回退取 ES row 的 tryon_image，再回退 display_image，确保是真实 URL。
    """
    sid = _item_sku_id(item)
    if sid.startswith(_IMG_ANCHOR_PREFIX):
        return image_url or ""
    raw = str(item.get("tryon_image") or "").strip()
    if raw and not raw.startswith(_DATA_URI_PREFIX):
        return raw
    # 被上传图覆盖或为空 → 从 ES row 取真实图
    if row:
        for key in ("tryon_image", "display_image"):
            v = str(row.get(key) or "").strip()
            if v and not v.startswith(_DATA_URI_PREFIX):
                return v
    # 兜底：item 的 display_image（即使是 data URI 也返回，避免空）
    return str(item.get("display_image") or "").strip()


def reshape_outfits_to_external(
    cards: list[dict[str, Any]],
    *,
    input_sku_id: str,
    image_url: str | None,
    session_id: str,
    data_facade: Any,
) -> dict[str, Any]:
    """把 chat_stream 产出的 outfit cards 整形为文档定义的对外出参。"""
    # 批量取所有 item 的 SKU 行（id_goods + 真实图回退）
    all_sku_ids: list[str] = []
    for card in cards or []:
        for it in card.get("items") or []:
            sid = _item_sku_id(it)
            if sid:
                all_sku_ids.append(sid)
    row_map: dict[str, dict[str, Any]] = {}
    if all_sku_ids:
        try:
            rows = data_facade.get_skus(all_sku_ids)
            row_map = {
                str(r.get("sku_id") or ""): r for r in (rows or []) if r
            }
        except Exception:
            logger.warning("[对外接口·reshape] 批量取 SKU 行失败，id_goods 将缺失", exc_info=True)

    outfits_out: list[dict[str, Any]] = []
    for rank, card in enumerate(cards or []):
        items_out: list[dict[str, Any]] = []
        for it in card.get("items") or []:
            sid = _item_sku_id(it)
            row = row_map.get(sid)
            items_out.append({
                "sku_id": sid,
                "spu_id": it.get("spu_id") or (row.get("spu_id") if row else None),
                "id_goods": str(row.get("id_goods") or "") if row else "",
                "role": it.get("role"),
                "title": it.get("title"),
                "price": it.get("price"),
                "sku_image_url": _resolve_sku_image_url(it, row, image_url),
            })
        outfits_out.append({
            "outfit_id": card.get("outfit_id"),
            "outfit_rank": rank,
            "items": items_out,
            "outfit_tryon_image": card.get("outfit_tryon_image") or "",
            "reason": card.get("reason") or "",
        })

    return {
        "session_id": session_id,
        "input_sku_id": input_sku_id or "",
        "outfits": outfits_out,
    }
