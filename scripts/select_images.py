#!/usr/bin/env python3
"""FILA：为 skus.jsonl 选择 display / index_images / tryon 图。

优先级：
  1. data/tables/fila_sku_selected_images.csv（货号 -> tryon_url + index_images 数组）
  2. product_image：同 id_goods + id_pa 的 cd / master / big
  3. product_master.image 作 display 兜底
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.etl_common import (
    EtlLogger,
    load_ai_select_index,
    load_csv,
    load_vlm_excluded_skus,
    load_vlm_index,
    norm_id_pa,
    processed_dir,
    product_dir_path,
    reports_dir,
    text_or_empty,
)
from backend.empty_image_urls import (
    is_empty_product_image_url,
    sku_has_empty_tryon_image,
)


def _image_index(prod: Path) -> dict[tuple[str, str], list[dict[str, str]]]:
    by_key: dict[tuple[str, str], list[dict[str, str]]] = {}
    for im in load_csv(prod / "product_image.csv"):
        if text_or_empty(im.get("status", "1")) != "1":
            continue
        gid = text_or_empty(im.get("id_goods"))
        pa = norm_id_pa(im.get("id_pa"))
        by_key.setdefault((gid, pa), []).append(im)
    for key in by_key:
        by_key[key].sort(
            key=lambda r: int(float(r.get("order_id") or 0)),
        )
    return by_key


def _pick_from_images(
    by_key: dict[tuple[str, str], list[dict[str, str]]],
    gid: str,
    pa: str,
) -> tuple[str, str, str]:
    lst = by_key.get((gid, pa), [])
    cd = [x for x in lst if text_or_empty(x.get("image_type")) == "cd"]
    master = [x for x in lst if text_or_empty(x.get("image_type")) == "master"]
    big = [x for x in lst if text_or_empty(x.get("image_type")) == "big"]
    disp = cd[0]["path"] if cd else (big[0]["path"] if big else "")
    idx = master[0]["path"] if master else (cd[0]["path"] if cd else "")
    tryon = idx
    return disp, idx, tryon


def _all_images_from_bucket(lst: list[dict[str, str]]) -> list[dict[str, object]]:
    """同 id_goods + id_pa 的全部 product_image 行（供详情页 / ES 索引）。"""
    out: list[dict[str, object]] = []
    for im in lst:
        path = text_or_empty(im.get("path"))
        if not path:
            continue
        try:
            order_id = int(float(im.get("order_id") or 0))
        except (TypeError, ValueError):
            order_id = 0
        out.append({
            "path": path,
            "id_pa": norm_id_pa(im.get("id_pa")),
            "order_id": order_id,
            "image_type": text_or_empty(im.get("image_type")),
        })
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="FILA select SKU images")
    parser.add_argument("--product-dir", type=Path, default=None)
    args = parser.parse_args()
    prod = args.product_dir or product_dir_path()
    out_dir = processed_dir()
    skus_path = out_dir / "skus.jsonl"
    if not skus_path.is_file():
        raise SystemExit(f"缺少 {skus_path}，请先运行 build_catalog.py")

    log = EtlLogger("image_selection")
    vlm = load_vlm_index(prod)
    excluded_skus = load_vlm_excluded_skus(prod)
    ai_by_sku, ai_by_spu = load_ai_select_index(prod)
    by_key = _image_index(prod)
    masters = {
        text_or_empty(r.get("id_goods")): r
        for r in load_csv(prod / "product_master.csv")
    }

    rows: list[dict] = []
    with skus_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    issues: list[str] = []
    idx_ok = 0
    tryon_ok = 0
    ai_ok = 0
    excluded_dropped = 0
    for row in rows:
        sid = text_or_empty(row.get("sku_id"))
        spu = text_or_empty(row.get("spu_id"))
        gid = str(row.get("id_goods") or row.get("goods_id") or "")
        pa = norm_id_pa(row.get("id_pa"))
        bucket = by_key.get((gid, pa), [])
        disp, idx_fallback, tryon_fallback = _pick_from_images(by_key, gid, pa)
        # ── VLM 覆盖（load_vlm_index 返回 dict: {tryon_url, index_images}）──
        vlm_entry = vlm.get(sid)
        if vlm_entry:
            tryon_url = vlm_entry.get("tryon_url") or ""
            index_images = list(vlm_entry.get("index_images") or [])
        else:
            tryon_url = ""
            index_images = []
        # VLM 已跑且全排除（吊牌/水洗标等非商品图）：不得 fallback 选回吊牌图，
        # 直接留空 → 走占位图过滤整条剔除，避免无可用主图的 SKU 入库。
        if sid in excluded_skus:
            excluded_dropped += 1
            index_images = []
            tryon_url = ""
        # VLM 无 index_images 时 fallback: product_image master 列表
        if not index_images:
            master = [
                x for x in bucket
                if text_or_empty(x.get("image_type")) == "master"
            ]
            cd = [
                x for x in bucket
                if text_or_empty(x.get("image_type")) == "cd"
            ]
            fb = master[0]["path"] if master else (cd[0]["path"] if cd else "")
            index_images = [fb] if fb else []
        if not disp:
            pm = masters.get(gid, {})
            disp = text_or_empty(pm.get("image"))
        row["display_image"] = disp
        row["index_images"] = index_images
        row["tryon_image"] = tryon_url or (index_images[0] if index_images else "")
        row["all_images"] = _all_images_from_bucket(bucket)
        ai_row = ai_by_sku.get(sid) or ai_by_spu.get(spu)
        if ai_row:
            row["ai_select"] = dict(ai_row)
            ai_ok += 1
        else:
            row.pop("ai_select", None)
        iq = row.get("image_quality") or {}
        iq["display_score"] = 1.0 if disp else 0.0
        iq["index_score"] = 1.0 if index_images else 0.0
        iq["tryon_score"] = 1.0 if row["tryon_image"] else 0.0
        iq["is_tryon_ready"] = bool(row["tryon_image"])
        row["image_quality"] = iq
        if index_images:
            idx_ok += 1
        if row["tryon_image"]:
            tryon_ok += 1
        if not disp:
            issues.append(f"- missing_display: {sid}")
        if not index_images:
            issues.append(f"- missing_index_images: {sid}")
        log.emit(
            "image_selected",
            {
                "sku_id": sid,
                "has_vlm": vlm_entry is not None,
                "has_index": bool(index_images),
                "index_images_count": len(index_images),
            },
        )

    filtered_empty_tryon = 0
    filtered_empty_display = 0
    kept_rows: list[dict] = []
    for row in rows:
        if sku_has_empty_tryon_image(row):
            filtered_empty_tryon += 1
            continue
        # display_image 为占位图也剔除：无可用展示图的 SKU 不应入库
        if is_empty_product_image_url(str(row.get("display_image") or "")):
            filtered_empty_display += 1
            continue
        kept_rows.append(row)

    with skus_path.open("w", encoding="utf-8") as handle:
        for row in kept_rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    report = [
        "# 图片质量报告",
        "",
        f"- run_id: `{log.run_id}`",
        f"- index_images 覆盖: {idx_ok}/{len(rows)}",
        f"- tryon_image 覆盖: {tryon_ok}/{len(rows)}",
        f"- ai_select 覆盖: {ai_ok}/{len(rows)}",
        f"- VLM 全排除剔除: {excluded_dropped}/{len(rows)}",
        f"- 过滤占位 tryon_image: {filtered_empty_tryon}/{len(rows)}",
        f"- 过滤占位 display_image: {filtered_empty_display}/{len(rows)}",
        "",
    ]
    if issues:
        report.append("## 问题样本（前 500）")
        report.extend(issues[:500])
    (reports_dir() / "image_quality_report.md").write_text(
        "\n".join(report) + "\n",
        encoding="utf-8",
    )
    log.emit(
        "image_selection_summary",
        {
            "total": len(rows),
            "index_ok": idx_ok,
            "tryon_ok": tryon_ok,
            "excluded_dropped": excluded_dropped,
            "filtered_empty_tryon": filtered_empty_tryon,
            "filtered_empty_display": filtered_empty_display,
            "kept": len(kept_rows),
            "issues": len(issues),
        },
    )
    log.close()
    print(
        "select_images done",
        f"index={idx_ok}/{len(rows)}",
        f"excluded_dropped={excluded_dropped}",
        f"filtered_empty_tryon={filtered_empty_tryon}",
        f"filtered_empty_display={filtered_empty_display}",
        f"kept={len(kept_rows)}/{len(rows)}",
        f"issues={len(issues)}",
    )


if __name__ == "__main__":
    main()
