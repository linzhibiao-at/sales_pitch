#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""过滤 ``fila_sku_selected_images.csv`` 中 ``white_front_url`` 为占位图的行。

读取 ``fila_images_preprocess.py`` 产出的 CSV，剔除
``backend.empty_image_urls.is_empty_product_image_url`` 判定为占位图的记录，
默认覆盖原文件。
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

import polars as pl

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.empty_image_urls import is_empty_product_image_url
from scripts._project_paths import load_paths as _load_paths

_PATHS = _load_paths()
DEFAULT_CSV = _PATHS["product_dir"] / "fila_sku_selected_images.csv"
WHITE_FRONT_URL_COL = "white_front_url"


def filter_empty_white_front_rows(df: pl.DataFrame) -> pl.DataFrame:
    """保留 ``white_front_url`` 非占位图的行（polars 多线程 map）。"""
    is_empty = pl.col(WHITE_FRONT_URL_COL).map_elements(
        is_empty_product_image_url,
        return_dtype=pl.Boolean,
        strategy="threading",
    )
    return df.filter(~is_empty)


def write_csv_atomic(df: pl.DataFrame, path: Path) -> None:
    """写入 CSV（UTF-8 BOM），完成后原子替换目标文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        suffix=".csv.tmp",
        dir=str(path.parent),
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        df.write_csv(tmp_path, include_bom=True)
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "过滤 fila_sku_selected_images.csv 中 white_front_url 为占位图的行"
            "（polars 并行），默认覆盖原文件"
        ),
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_CSV,
        help=f"输入 CSV，默认 {DEFAULT_CSV}",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="输出 CSV，默认与 --input 相同（覆盖）",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=0,
        help="polars 线程数（0 表示使用 polars 默认）",
    )
    args = parser.parse_args()

    if args.threads > 0:
        os.environ["POLARS_MAX_THREADS"] = str(args.threads)

    venv_hint = (
        f"已激活虚拟环境: {os.environ.get('VIRTUAL_ENV')}"
        if os.environ.get("VIRTUAL_ENV")
        else "未检测到 VIRTUAL_ENV"
    )
    print(f"filter_fila_sku_selected_images_empty  ({venv_hint})")

    input_path = args.input
    if not input_path.is_file():
        print(f"错误：文件不存在 {input_path}", file=sys.stderr)
        return 1

    output_path = args.output or input_path

    df = pl.read_csv(
        input_path,
        encoding="utf8-lossy",
        infer_schema_length=0,
        truncate_ragged_lines=True,
    )
    if WHITE_FRONT_URL_COL not in df.columns:
        print(
            f"错误：缺少列 {WHITE_FRONT_URL_COL!r}，"
            f"当前列: {df.columns}",
            file=sys.stderr,
        )
        return 1

    before = df.height
    filtered = filter_empty_white_front_rows(df)
    after = filtered.height
    removed = before - after

    write_csv_atomic(filtered, output_path)

    print(f"输入: {input_path}")
    print(f"输出: {output_path}（{'覆盖' if output_path == input_path else '写入'}）")
    print(f"总行数: {before}  移除占位图: {removed}  保留: {after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
