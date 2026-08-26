#!/usr/bin/env python3
"""分批从 MySQL 拉取 cc_material_product 全量数据并导出为 CSV。

依赖: pip3 install pymysql -i https://mirrors.aliyun.com/pypi/simple/

用法示例:
  export CC_MYSQL_PASSWORD='your-password'
  python3 pull_cc_material_product.py

也可通过命令行覆盖连接参数:
  python3 pull_cc_material_product.py --batch-size 5000
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Tuple

import pymysql
from pymysql.cursors import DictCursor

TABLE_NAME = "cc_material_product"
PRIMARY_KEY = "material_product_id"
DEFAULT_COLUMNS = (
    "material_product_id",
    "material_id",
    "article_no",
    "product_name",
    "create_by",
    "create_time",
    "modify_by",
    "modify_time",
)

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
import sys

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts._project_paths import load_paths

DEFAULT_OUTPUT = load_paths()["product_dir"] / "cc_material_product_all.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="分批拉取 cc_material_product 全量数据到 CSV",
    )
    parser.add_argument("--host", default=os.getenv("CC_MYSQL_HOST", "10.128.0.71"))
    parser.add_argument("--port", type=int, default=int(os.getenv("CC_MYSQL_PORT", "3306")))
    parser.add_argument(
        "--database",
        default=os.getenv("CC_MYSQL_DATABASE", "ry-cloud"),
    )
    parser.add_argument("--user", default=os.getenv("CC_MYSQL_USER", "usr_ai"))
    parser.add_argument(
        "--password",
        default=os.getenv("CC_MYSQL_PASSWORD", ""),
        help="优先使用环境变量 CC_MYSQL_PASSWORD",
    )
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="输出 CSV 路径，默认 data/tables/cc_material_product_all.csv",
    )
    return parser.parse_args()


def connect_mysql(args: argparse.Namespace) -> pymysql.connections.Connection:
    if not args.password:
        logger.error(
            "未设置数据库密码，请 export CC_MYSQL_PASSWORD=... "
            "或使用 --password",
        )
        sys.exit(1)
    return pymysql.connect(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
        charset="utf8mb4",
        cursorclass=DictCursor,
        connect_timeout=30,
        read_timeout=600,
        write_timeout=600,
    )


def fetch_table_columns(conn: pymysql.connections.Connection) -> List[str]:
    sql = (
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
        "ORDER BY ORDINAL_POSITION"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (conn.db.decode() if isinstance(conn.db, bytes) else conn.db, TABLE_NAME))
        rows = cur.fetchall()
    if not rows:
        logger.warning("未从 information_schema 读到列，使用默认列名")
        return list(DEFAULT_COLUMNS)
    return [row["COLUMN_NAME"] for row in rows]


def normalize_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, Decimal):
        return format(value, "f").rstrip("0").rstrip(".") or "0"
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def fetch_batch(
    conn: pymysql.connections.Connection,
    columns: Sequence[str],
    last_id: int,
    batch_size: int,
) -> List[dict]:
    col_sql = ", ".join(f"`{c}`" for c in columns)
    sql = (
        f"SELECT {col_sql} FROM `{TABLE_NAME}` "
        f"WHERE `{PRIMARY_KEY}` > %s "
        f"ORDER BY `{PRIMARY_KEY}` ASC LIMIT %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (last_id, batch_size))
        return list(cur.fetchall())


def count_rows(conn: pymysql.connections.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS cnt FROM `{TABLE_NAME}`")
        row = cur.fetchone()
    return int(row["cnt"]) if row else 0


def write_rows(
    writer: csv.DictWriter,
    rows: Iterable[dict],
    columns: Sequence[str],
) -> int:
    n = 0
    for row in rows:
        writer.writerow(
            {col: normalize_cell(row.get(col)) for col in columns},
        )
        n += 1
    return n


def export_all(args: argparse.Namespace) -> Tuple[Path, int, int]:
    conn = connect_mysql(args)
    try:
        total_expected = count_rows(conn)
        logger.info("表 %s 预估行数: %d", TABLE_NAME, total_expected)

        columns = fetch_table_columns(conn)
        logger.info("导出列 (%d): %s", len(columns), ", ".join(columns))

        args.output.parent.mkdir(parents=True, exist_ok=True)
        last_id = 0
        exported = 0
        batch_no = 0

        with args.output.open("w", encoding="utf-8-sig", newline="") as fp:
            writer = csv.DictWriter(fp, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()

            while True:
                batch_no += 1
                rows = fetch_batch(conn, columns, last_id, args.batch_size)
                if not rows:
                    break

                batch_count = write_rows(writer, rows, columns)
                exported += batch_count
                last_id = int(rows[-1][PRIMARY_KEY])

                logger.info(
                    "批次 %d: 本批 %d 行, 累计 %d / %d, last_%s=%d",
                    batch_no,
                    batch_count,
                    exported,
                    total_expected,
                    PRIMARY_KEY,
                    last_id,
                )

                if batch_count < args.batch_size:
                    break

        if exported != total_expected:
            logger.warning(
                "导出行数 (%d) 与 COUNT(*) (%d) 不一致，请核对数据或主键",
                exported,
                total_expected,
            )

        return args.output, exported, total_expected
    finally:
        conn.close()


def main() -> None:
    args = parse_args()
    output, exported, expected = export_all(args)
    logger.info(
        "完成: %s (%d 行, 预估 %d 行)",
        output,
        exported,
        expected,
    )


if __name__ == "__main__":
    main()
