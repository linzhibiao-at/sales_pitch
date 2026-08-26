#!/usr/bin/env python3
"""顺序执行 FILA 统一离线 ETL。

支持 --incremental 模式：仅对 updated_at 变化的款号重建目录，并跳过
ES/Milvus 中内容未变的文档写入（依赖 data/logs/fila_index_sync_state.json）。

用法（在 fila_agent_html 目录）::

  # 全量（默认，向后兼容）
  python3 scripts/run_processed_etl.py

  # 增量
  python3 scripts/run_processed_etl.py --incremental

  # 从第 3 步开始
  python3 scripts/run_processed_etl.py --from-step 3
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _build_steps(incremental: bool) -> list[tuple[str, str]]:
    """Return the ordered list of (script, label) steps."""
    steps: list[tuple[str, str]] = [
        ("build_catalog.py", "目录 skus.jsonl"),
        ("select_images.py", "选图"),
        ("build_fila_guide_outfits_fast.py", "固定搭配 fila_outfits.json"),
        ("build_fila_es_index.py", "构建 ES 索引"),
        ("build_fila_milvus_multimodal_index.py", "构建 Milvus 多图向量索引"),
        ("build_text_milvus_index.py", "构建 Milvus 文本向量索引"),
    ]
    return steps


def _step_extra_args(
    script: str,
    incremental: bool,
    product_dir_args: list[str],
    guide_workers: int,
) -> list[str]:
    """Return extra CLI args for a given step."""
    if script == "build_catalog.py":
        extra = list(product_dir_args)
        if incremental:
            extra.append("--incremental")
        return extra
    if script == "build_fila_guide_outfits_fast.py":
        return ["--workers", str(guide_workers)]
    if script == "select_images.py":
        return list(product_dir_args)
    if script == "build_fila_es_index.py":
        return ["--incremental", "--prune-orphans"] if incremental else []
    if script == "build_fila_milvus_multimodal_index.py":
        return ["--incremental", "--prune-orphans"] if incremental else []
    if script == "build_text_milvus_index.py":
        return ["--incremental", "--prune-orphans"] if incremental else []
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Run FILA unified ETL pipeline")
    parser.add_argument(
        "--from-step",
        type=int,
        default=1,
        help="从第几步开始（1=build_catalog）",
    )
    parser.add_argument(
        "--product-dir",
        type=str,
        default="",
    )
    parser.add_argument(
        "--guide-workers",
        type=int,
        default=64,
        help="build_fila_guide_outfits_fast.py 的并发 worker 数",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help=(
            "增量模式：对 build_catalog 传入 --incremental，"
            "对 ES/Milvus 索引构建传入 --incremental --prune-orphans"
        ),
    )
    args = parser.parse_args()

    product_dir_args: list[str] = (
        ["--product-dir", args.product_dir.strip()]
        if args.product_dir.strip()
        else []
    )

    steps = _build_steps(args.incremental)
    mode_label = "INCREMENTAL" if args.incremental else "FULL"
    print(f"ETL 模式: {mode_label}")

    for i, (script, label) in enumerate(steps, start=1):
        if i < args.from_step:
            continue
        extra = _step_extra_args(
            script, args.incremental, product_dir_args, args.guide_workers,
        )
        path = ROOT / "scripts" / script
        print(f"\n=== [{i}/{len(steps)}] {label}: {script} ===")
        rc = subprocess.call(
            [sys.executable, str(path), *extra],
            cwd=str(ROOT),
        )
        if rc != 0:
            print(f"失败: {script} exit={rc}")
            return rc

    print("\nETL 完成。可运行: python3 scripts/validate_data.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
