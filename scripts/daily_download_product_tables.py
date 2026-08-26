#!/usr/bin/env python3
"""每日从大数据 Hive 数据服务拉取 FILA 商品相关表到 data/tables/。

依赖:
    pip3 install requests -i https://mirrors.aliyun.com/pypi/simple/

凭证（勿写进代码）:
    export HIVE_USERNAME=u_mgs
    export HIVE_PASSWORD='your-password'

用法:
    cd fila_agent_html
    python3 scripts/daily_download_product_tables.py --env prod

    # 只拉部分表
    python3 scripts/daily_download_product_tables.py --tables product_sku,product_master

    # 定时任务示例 (crontab, 每天 02:00)
    # 0 2 * * * cd /path/to/fila_agent_html && python3 scripts/daily_download_product_tables.py --env prod >> data/logs/daily_download.log 2>&1
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import zipfile
from datetime import date
from pathlib import Path

# 将 fila_agent_html 加入 path，便于 import backend
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.hive_query import run_query_and_download

# 表名 -> 数据服务 code -> 产出 CSV（相对 data/tables）
HIVE_TABLE_JOBS: list[dict[str, str]] = [
    {
        "table": "product_master",
        "code": "fc69330a71cd1b5ce6ae",
        "csv": "product_master.csv",
    },
    {
        "table": "product_master_ext",
        "code": "c8e5cf8ef3061ea6fb0f",
        "csv": "product_master_ext.csv",
    },
    {
        "table": "product_attr",
        "code": "d5769bd4470044451f28",
        "csv": "product_attr.csv",
    },
    {
        "table": "product_sku",
        "code": "e2916954997ec19b16e1",
        "csv": "product_sku.csv",
    },
    {
        "table": "product_image",
        "code": "6fef90b3b81f18712bda",
        "csv": "product_image.csv",
    },
    {
        "table": "product_image_type",
        "code": "09282def8386f9f76838",
        "csv": "product_image_type.csv",
    },
    {
        "table": "search_index",
        "code": "0eab71931ff8a63d6fff",
        "csv": "search_index.csv",
    },
    {
        "table": "product_guide_recommend",
        "code": "5aa0e1b58db2a38b2e38",
        "csv": "product_guide_recommend.csv",
    },
    {
        "table": "product_guide_recommend_ext",
        "code": "c44b4898ece34c0e10b4",
        "csv": "product_guide_recommend_ext.csv",
    },
    {
        "table": "cc_material_product",
        "code": "de17169fb98edc7e2d98",
        "csv": "cc_material_product.csv",
    },
]

DEFAULT_TABLES_DIR = _PROJECT_ROOT / "data" / "tables"

# FILA 商品 xlsx 导出产物（anta AOP API 拉取，由 export_main_fila_products_prod 复用）
FILA_PRODUCTS_XLSX = "fila_products_brief_prod.xlsx"
DEFAULT_FILA_PRODUCT_YEARS = 2


def _list_download_csvs(download_dir: Path) -> list[Path]:
    """列出 Hive 下载目录中的全部 CSV（含子目录）。"""
    candidates = sorted(download_dir.glob("*.csv"))
    if not candidates:
        candidates = sorted(download_dir.rglob("*.csv"))
    return candidates


def _normalize_csv_header_line(line: str) -> str:
    return line.lstrip("\ufeff").rstrip("\r\n")


def _merge_csv_files(sources: list[Path], dest: Path) -> Path:
    """将多个分片 CSV 合并为一个文件；单文件时直接返回原路径。"""
    if not sources:
        raise ValueError("无可合并的 CSV 文件")
    if len(sources) == 1:
        return sources[0]

    dest.parent.mkdir(parents=True, exist_ok=True)
    header: str | None = None
    with dest.open("w", encoding="utf-8-sig", newline="") as out_fp:
        for idx, src in enumerate(sources):
            with src.open("r", encoding="utf-8-sig", newline="") as in_fp:
                first_line = in_fp.readline()
                if not first_line:
                    continue
                if idx == 0:
                    header = _normalize_csv_header_line(first_line)
                    out_fp.write(first_line)
                elif _normalize_csv_header_line(first_line) != header:
                    out_fp.write(first_line)
                shutil.copyfileobj(in_fp, out_fp)
    return dest


def _publish_csv(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    shutil.copy2(src, tmp)
    tmp.replace(dest)


def _zip_tables_dir(tables_dir: Path, zip_path: Path) -> Path:
    """将 tables_dir 整体打包为 zip（输出到 tables_dir 外部，避免自包含）。"""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = zip_path.with_suffix(zip_path.suffix + ".part")
    if tmp.exists():
        tmp.unlink()
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(tables_dir.rglob("*")):
            if p.is_file() and p != tmp:
                zf.write(p, p.relative_to(tables_dir))
    tmp.replace(zip_path)
    return zip_path


def parse_args() -> argparse.Namespace:
    all_tables = [j["table"] for j in HIVE_TABLE_JOBS]
    parser = argparse.ArgumentParser(
        description="每日下载 FILA 商品 Hive 表到 data/tables",
    )
    parser.add_argument(
        "--env",
        choices=["prod", "uat", "outer", "office"],
        default="prod",
        help="大数据环境 (默认: prod)",
    )
    parser.add_argument(
        "-u",
        "--username",
        default=os.environ.get("HIVE_USERNAME"),
        help="用户名，或环境变量 HIVE_USERNAME",
    )
    parser.add_argument(
        "-p",
        "--password",
        default=os.environ.get("HIVE_PASSWORD"),
        help="密码，或环境变量 HIVE_PASSWORD",
    )
    parser.add_argument(
        "--tables-dir",
        type=Path,
        default=DEFAULT_TABLES_DIR,
        help="商品 CSV 目录，默认 fila_agent_html/data/tables",
    )
    parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="快照日期目录名 YYYY-MM-DD (默认: 今天)",
    )
    parser.add_argument(
        "--tables",
        default="",
        help="逗号分隔表名，空则下载全部",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=300,
        help="单表查询轮询间隔/秒 (默认: 300)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="单表查询超时/秒 (默认: 3600)",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="若当日快照目录已有文件则跳过该表",
    )
    parser.add_argument(
        "--no-publish",
        action="store_true",
        help="仅保留 daily 快照，不覆盖 tables_dir 下最新 CSV",
    )
    parser.add_argument(
        "--skip-fila-products",
        action="store_true",
        help="跳过 FILA 商品 xlsx 导出（anta AOP SP_M_SPZSJ_QUERY，默认执行）",
    )
    parser.add_argument(
        "--fila-products-years",
        type=int,
        default=DEFAULT_FILA_PRODUCT_YEARS,
        help="FILA 商品 LAST_UPDATE_DATE 回溯年数 (默认: 2)",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="任一张表失败则立即退出",
    )
    parser.add_argument(
        "--zip-tables",
        action="store_true",
        help="下载完成后将 tables 目录打包为 data/tables.zip（默认不打包）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印计划，不实际下载",
    )
    parser.add_argument(
        "--list-tables",
        action="store_true",
        help="列出可下载表并退出",
    )
    return parser.parse_args()


def _select_jobs(table_filter: str) -> list[dict[str, str]]:
    if not table_filter.strip():
        return list(HIVE_TABLE_JOBS)
    wanted = {t.strip() for t in table_filter.split(",") if t.strip()}
    jobs = [j for j in HIVE_TABLE_JOBS if j["table"] in wanted]
    unknown = wanted - {j["table"] for j in jobs}
    if unknown:
        raise ValueError(f"未知表名: {', '.join(sorted(unknown))}")
    return jobs


def _run_fila_product_export(output_path: Path, years: int) -> int:
    """调用 export_main_fila_products_prod.run_export 生成 FILA 商品 xlsx。

    延迟 import：daily 下载本身不依赖 openpyxl，仅在此阶段才需要。
    """
    if str(_SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPT_DIR))
    from export_main_fila_products_prod import run_export  # noqa: WPS433
    return run_export(output_file=str(output_path), years=years)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("daily_download")

    args = parse_args()
    if args.list_tables:
        for j in HIVE_TABLE_JOBS:
            print(f"{j['table']}\t{j['code']}\t{j['csv']}")
        return 0

    if not args.username or not args.password:
        logger.error(
            "请设置 HIVE_USERNAME / HIVE_PASSWORD，或使用 -u / -p",
        )
        return 1

    try:
        jobs = _select_jobs(args.tables)
    except ValueError as exc:
        logger.error("%s", exc)
        return 1

    daily_root = args.tables_dir / "daily" / args.date
    failed: list[str] = []

    logger.info(
        "计划下载 %d 张表, env=%s, tables_dir=%s, snapshot=%s",
        len(jobs),
        args.env,
        args.tables_dir,
        daily_root,
    )

    for job in jobs:
        table = job["table"]
        code = job["code"]
        csv_name = job["csv"]
        snapshot_dir = daily_root / table
        publish_path = args.tables_dir / csv_name

        if args.skip_existing and snapshot_dir.is_dir():
            existing = list(snapshot_dir.iterdir())
            if existing:
                logger.info("跳过 %s (快照目录非空)", table)
                continue

        logger.info("==== %s (code=%s) ====", table, code)
        if args.dry_run:
            logger.info(
                "  dry-run: 下载 -> %s, 发布 -> %s",
                snapshot_dir,
                publish_path,
            )
            continue

        if snapshot_dir.exists():
            shutil.rmtree(snapshot_dir)
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        try:
            paths = run_query_and_download(
                env_key=args.env,
                username=args.username,
                password=args.password,
                code=code,
                params={},
                output_dir=str(snapshot_dir),
                poll_interval=args.poll_interval,
                timeout=args.timeout,
                verbose=True,
            )
            if not paths:
                raise RuntimeError("无 HDFS 文件可下载（可能结果在 OBS）")

            candidates = _list_download_csvs(snapshot_dir)
            if not candidates:
                raise RuntimeError(f"未在 {snapshot_dir} 找到 CSV 文件")

            src_csv = _merge_csv_files(
                candidates,
                snapshot_dir / "_merged.csv",
            )
            if len(candidates) > 1:
                logger.info(
                    "已合并 %d 个 CSV -> %s",
                    len(candidates),
                    src_csv,
                )

            if not args.no_publish:
                _publish_csv(src_csv, publish_path)
                logger.info("已发布: %s", publish_path)
            else:
                logger.info("未发布 (--no-publish), 快照: %s", snapshot_dir)

        except Exception as exc:
            logger.exception("表 %s 下载失败: %s", table, exc)
            failed.append(table)
            if args.fail_fast:
                break

    # ── FILA 商品 xlsx 导出（anta AOP API，独立于 Hive 下载）──────────
    fila_output = args.tables_dir / FILA_PRODUCTS_XLSX
    if args.skip_fila_products:
        logger.info("跳过 FILA 商品导出 (--skip-fila-products)")
    elif args.dry_run:
        logger.info("==== FILA 商品导出 (dry-run) ====")
        logger.info("  dry-run: 导出 -> %s", fila_output)
    else:
        logger.info("==== FILA 商品导出 (anta AOP, 最近 %d 年) ====",
                    args.fila_products_years)
        try:
            rows = _run_fila_product_export(fila_output, args.fila_products_years)
            if rows:
                logger.info("已导出 FILA 商品 xlsx: %s (%d 行)", fila_output, rows)
            else:
                logger.warning("FILA 商品导出未产出数据行: %s", fila_output)
        except Exception as exc:
            logger.exception("FILA 商品导出失败: %s", exc)
            failed.append("fila_products_brief_prod")
            if args.fail_fast:
                # 仍执行下面的清理逻辑再退出
                pass

    if failed:
        logger.error("失败项: %s", ", ".join(failed))
        return 1

    # ── 下载完毕后清理 daily 快照目录（--no-publish 模式保留）──────────
    daily_root_parent = args.tables_dir / "daily"
    if not args.dry_run:
        if not args.no_publish and daily_root_parent.exists():
            shutil.rmtree(daily_root_parent, ignore_errors=True)
            logger.info("已删除 daily 快照目录: %s", daily_root_parent)
        if args.zip_tables:
            zip_path = _zip_tables_dir(
                args.tables_dir, args.tables_dir.parent / "tables.zip",
            )
            logger.info("已打包: %s", zip_path)

    logger.info("全部完成 (%d 张表)", len(jobs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
