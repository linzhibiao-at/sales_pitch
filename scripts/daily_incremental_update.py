#!/usr/bin/env python3
"""FILA 每日增量更新编排器。

将下载、目录构建、图片选择、ES/Milvus 索引构建串接为一条流水线。
默认使用增量模式（依赖 product_master.updated_at + index_sync_state）；
--full 回退为全量重建。

用法（在 fila_agent_html 目录）::

  # 每日增量（跳过下载，假设 CSV 已就绪）
  python3 scripts/daily_incremental_update.py --skip-download

  # 含 Hive 下载的完整增量
  python3 scripts/daily_incremental_update.py --env prod

  # 全量重建（含搭配）
  python3 scripts/daily_incremental_update.py --full --env prod

  # 只打印计划
  python3 scripts/daily_incremental_update.py --dry-run

定时任务（crontab）::

  # 周一至周六 03:00 增量（不含搭配）
  0 3 * * 1-6 cd /path/to/fila_agent_html && python3 scripts/daily_incremental_update.py --env prod --skip-outfits >> data/logs/daily_cron.log 2>&1
  # 周日 03:00 全量（含搭配）
  0 3 * * 0   cd /path/to/fila_agent_html && python3 scripts/daily_incremental_update.py --full --env prod >> data/logs/daily_cron.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
LOGS_DIR = ROOT / "data" / "logs"


def _ts() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")


def _run_step(
    label: str,
    script: str,
    extra_args: list[str],
    *,
    dry_run: bool,
    cwd: Path = ROOT,
) -> tuple[int, float]:
    """Run a single pipeline step. Returns (exit_code, elapsed_seconds)."""
    cmd = [sys.executable, str(SCRIPTS_DIR / script), *extra_args]
    print(f"\n{'='*60}")
    print(f"[{_ts()}] {label}")
    print(f"  cmd: {' '.join(cmd)}")
    print(f"{'='*60}")
    if dry_run:
        print("  [dry-run] skipped")
        return 0, 0.0
    t0 = time.monotonic()
    rc = subprocess.call(cmd, cwd=str(cwd))
    elapsed = time.monotonic() - t0
    status = "OK" if rc == 0 else f"FAIL (exit={rc})"
    print(f"[{_ts()}] {label} — {status} ({elapsed:.1f}s)")
    return rc, elapsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FILA 每日增量更新编排器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python3 scripts/daily_incremental_update.py --skip-download\n"
            "  python3 scripts/daily_incremental_update.py --full --env prod\n"
        ),
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="全量模式：跳过增量逻辑，重建所有索引（含搭配）",
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="跳过 Hive 表下载（假设 CSV 已是最新）",
    )
    parser.add_argument(
        "--skip-outfits",
        action="store_true",
        help="跳过搭配索引构建（build_fila_guide_outfits_fast.py）",
    )
    parser.add_argument(
        "--env",
        choices=["prod", "uat", "outer", "office"],
        default="prod",
        help="Hive 大数据环境（默认: prod）",
    )
    parser.add_argument(
        "--product-dir",
        type=str,
        default="",
        help="商品 CSV 目录（覆盖 config paths.product_dir）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印执行计划，不实际运行",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    mode = "FULL" if args.full else "INCREMENTAL"
    started = datetime.now(timezone.utc).astimezone()

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"daily_update_{started.strftime('%Y%m%d_%H%M%S')}.jsonl"

    print(f"FILA Daily Update — mode={mode}  started={started.isoformat()}")
    print(f"Log: {log_path}")

    product_dir_args: list[str] = (
        ["--product-dir", args.product_dir]
        if args.product_dir.strip()
        else []
    )

    # ── Build step list ──────────────────────────────────────────────────
    steps: list[tuple[str, str, list[str]]] = []

    # Step 1: Hive download (optional)
    if not args.skip_download:
        steps.append((
            "1/7  下载 Hive 商品表",
            "daily_download_product_tables.py",
            ["--env", args.env],
        ))

    # Step 2: Catalog build
    catalog_extra = [] if args.full else ["--incremental"]
    catalog_extra += product_dir_args
    steps.append((
        "2/7  构建 skus.jsonl" + ("" if args.full else "（增量）"),
        "build_catalog.py",
        catalog_extra,
    ))

    # Step 3: Image selection (always full — fast, no API calls)
    steps.append((
        "3/7  选图（更新 display/index/tryon image URL）",
        "select_images.py",
        product_dir_args,
    ))

    # Step 4: ES index
    es_extra = [] if args.full else ["--incremental", "--prune-orphans"]
    steps.append((
        "4/7  构建 ES 索引" + ("" if args.full else "（增量）"),
        "build_fila_es_index.py",
        es_extra,
    ))

    # Step 5: Milvus image vector index
    milvus_extra = [] if args.full else ["--incremental", "--prune-orphans"]
    steps.append((
        "5/7  构建 Milvus 多图向量索引" + ("" if args.full else "（增量）"),
        "build_fila_milvus_multimodal_index.py",
        milvus_extra,
    ))

    # Step 6: Milvus text vector index
    text_extra = [] if args.full else ["--incremental", "--prune-orphans"]
    steps.append((
        "6/7  构建 Milvus 文本向量索引" + ("" if args.full else "（增量）"),
        "build_text_milvus_index.py",
        text_extra,
    ))

    # Step 7: Outfit build (optional; full mode or when explicitly not skipped)
    if not args.skip_outfits:
        steps.append((
            "7/7  构建搭配索引（微导购搭配）",
            "build_fila_guide_outfits_fast.py",
            ["--workers", "64"] + product_dir_args,
        ))

    # ── Execute ──────────────────────────────────────────────────────────
    step_results: list[dict] = []
    total_t0 = time.monotonic()
    failed_step: str | None = None

    for label, script, extra in steps:
        rc, elapsed = _run_step(
            label, script, extra, dry_run=args.dry_run,
        )
        step_results.append({
            "label": label,
            "script": script,
            "exit_code": rc,
            "elapsed_sec": round(elapsed, 1),
        })
        if rc != 0:
            failed_step = label
            print(f"\n[ERROR] 步骤失败，中止流水线: {label}")
            break

    total_elapsed = time.monotonic() - total_t0
    finished = datetime.now(timezone.utc).astimezone()

    # ── Summary ──────────────────────────────────────────────────────────
    summary = {
        "mode": mode,
        "started": started.isoformat(),
        "finished": finished.isoformat(),
        "total_elapsed_sec": round(total_elapsed, 1),
        "steps": step_results,
        "success": failed_step is None,
        "failed_step": failed_step,
    }

    print(f"\n{'='*60}")
    print("FILA Daily Update — Summary")
    print(f"{'='*60}")
    print(f"  Mode:    {mode}")
    print(f"  Started: {started.strftime('%H:%M:%S')}")
    print(f"  Ended:   {finished.strftime('%H:%M:%S')}")
    print(f"  Elapsed: {total_elapsed:.1f}s")
    for sr in step_results:
        icon = "✓" if sr["exit_code"] == 0 else "✗"
        print(f"  {icon} {sr['label']}  ({sr['elapsed_sec']}s)")
    if failed_step:
        print(f"\n  ✗ FAILED at: {failed_step}")
    else:
        print("\n  ✓ All steps completed successfully.")
    print(f"\nLog: {log_path}")

    # ── Write JSONL log ──────────────────────────────────────────────────
    if not args.dry_run:
        with log_path.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(summary, ensure_ascii=False) + "\n")

    return 1 if failed_step else 0


if __name__ == "__main__":
    raise SystemExit(main())
