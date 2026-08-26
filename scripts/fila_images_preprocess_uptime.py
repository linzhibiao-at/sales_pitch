#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FILA 图片预处理（up_time 过滤版）。

在 ``fila_images_preprocess.py`` 基础上增加过滤条件：
- product_master.onsell=1（在售）
- product_master.up_time >= 2023-01-01（上架时间不早于 2023-01-01）

只预处理同时满足以上两个条件的 SKU 图片，输出文件名保持不变
（默认 ``data/tables/fila_sku_selected_images.csv``）。

选图规则与 ``fila_images_preprocess.py`` 一致：
- tryon_image：纯色背景、无较大说明文字的商品静物主图，优先无模特，无无模特图时可接受模特少部分部位出现的纯色背景图，按 正面>侧面>背面 优先级；
- index_images：所有商品展示图，尽量优先纯色背景商品图。

实现方式：复用 ``fila_images_preprocess`` 的全部逻辑，仅替换
``load_onsale_skus`` 为带 up_time 过滤的版本，并强制 ``--source=onsale``。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, List

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import fila_images_preprocess as base  # noqa: E402
from fila_images_preprocess import (  # noqa: E402
    _norm_id,
    read_csv_dicts,
)

DEFAULT_UP_TIME = "2023-01-01"


def _parse_up_time(raw: Any) -> datetime:
    """解析 up_time 字段为 datetime，失败返回 datetime.min。"""
    if raw is None:
        return datetime.min
    s = str(raw).strip()
    if not s:
        return datetime.min
    # 常见格式：2024-04-08 21:04:54 或 2024-04-08
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    # 兜底：取前 10 位日期
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        return datetime.min


def load_onsale_skus_with_uptime(
    product_dir: Path,
    up_time_since: str = DEFAULT_UP_TIME,
) -> List[dict]:
    """加载在售且 up_time >= up_time_since 的 SKU。

    过滤条件：
    - product_master: onsell=1 且 up_time >= up_time_since
    - product_attr:   id_pac=1（颜色维度）、status=0（有效）

    返回格式与 base.load_onsale_skus 一致：[{attr_alias, id_goods, id_pa}]。
    """
    threshold = _parse_up_time(up_time_since)

    # 1. 加载在售且 up_time 达标的 id_goods 集合
    master_path = product_dir / "product_master.csv"
    qualified_gids: set[int] = set()
    for r in read_csv_dicts(master_path):
        gid = _norm_id(r.get("id_goods"))
        if gid is None:
            continue
        if not base._is_onsell(r.get("onsell")):
            continue
        if _parse_up_time(r.get("up_time")) < threshold:
            continue
        qualified_gids.add(gid)

    # 2. 从 product_attr 收集颜色维度 SKU
    attr_path = product_dir / "product_attr.csv"
    rows: List[dict] = []
    seen: set[str] = set()
    for r in read_csv_dicts(attr_path):
        gid = _norm_id(r.get("id_goods"))
        if gid is None or gid not in qualified_gids:
            continue
        if str(r.get("id_pac", "")).strip() != "1":
            continue
        if str(r.get("status", "0")).strip() != "0":
            continue
        alias = (r.get("attr_alias") or "").strip()
        if not alias or alias in seen:
            continue
        if base.is_legacy_sku_id(alias):
            continue
        seen.add(alias)
        id_pa = _norm_id(r.get("id_pa"))
        if id_pa is None:
            id_pa = 0
        rows.append(
            {
                "attr_alias": alias,
                "id_goods": gid,
                "id_pa": id_pa,
            }
        )
    return rows


def _print_help() -> None:
    print(
        "usage: fila_images_preprocess_uptime.py [-h] [--up-time-since UP_TIME_SINCE] "
        "[fila_images_preprocess 参数...]\n"
        "\n"
        "FILA 图片预处理（up_time 过滤版）：仅处理 onsell=1 "
        "且 up_time >= 2023-01-01 的 SKU，输出文件名保持不变\n"
        "\n"
        "options:\n"
        "  -h, --help            显示此帮助\n"
        "  --up-time-since DATE  up_time 下限日期（YYYY-MM-DD），默认 2023-01-01\n"
        "\n"
        "其余参数（--threads / --output / --limit / --config 等）透传给 fila_images_preprocess.py"
    )


def main() -> int:
    # 手动从 sys.argv 中提取 --up-time-since，其余原样透传给 base.main()。
    # 不用 argparse.REMAINDER：遇到未知 --option 会报错。
    up_time_since = DEFAULT_UP_TIME
    passthrough: List[str] = [sys.argv[0]]
    i = 1
    while i < len(sys.argv):
        a = sys.argv[i]
        if a in ("-h", "--help"):
            _print_help()
            return 0
        if a == "--up-time-since":
            if i + 1 >= len(sys.argv):
                print("错误：--up-time-since 需要参数", file=sys.stderr)
                return 2
            up_time_since = sys.argv[i + 1]
            i += 2
            continue
        if a.startswith("--up-time-since="):
            up_time_since = a.split("=", 1)[1]
            i += 1
            continue
        passthrough.append(a)
        i += 1

    # 替换原 loader 为带 up_time 过滤的版本
    base.load_onsale_skus = lambda pd: load_onsale_skus_with_uptime(
        pd, up_time_since=up_time_since
    )

    # 强制 --source=onsale
    if not any(a == "--source" or a.startswith("--source=") for a in passthrough):
        passthrough.extend(["--source", "onsale"])
    else:
        passthrough = _override_source(passthrough)

    sys.argv = passthrough
    return base.main()


def _override_source(argv: List[str]) -> List[str]:
    """把 argv 中的 --source 值强制改为 onsale。"""
    out: List[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--source":
            out.append("--source")
            out.append("onsale")
            i += 2  # 跳过原值
            continue
        if a.startswith("--source="):
            out.append("--source=onsale")
            i += 1
            continue
        out.append(a)
        i += 1
    return out


if __name__ == "__main__":
    raise SystemExit(main())
