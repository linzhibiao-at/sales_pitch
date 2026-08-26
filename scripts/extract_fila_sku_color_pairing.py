#!/usr/bin/env python3
"""从 FILA SKU 搭配色系 CSV 归纳色系配对规则，输出两份 YAML。

数据源: data/tables/fila_sku_outfit_colors.csv
其中 ``json`` 列形如 ``{"上装": "灰色系", "下装": "黑色系", "鞋": "白色系"}``。

统计两类配对，分别输出独立 YAML (格式与
backend/intent/dictionaries/color_series_pairing.yaml 一致):

  - 上装 → 下装  -> fila_sku_color_pairing_top_bottom.yaml
  - 下装 → 上装  -> fila_sku_color_pairing_bottom_top.yaml
  - 下装 → 鞋    -> fila_sku_color_pairing_bottom_shoe.yaml

跳过 ``error`` 列非空或 ``json`` 解析失败的错误记录。
每条规则字段: anchor_count(锚点统计数) / primary_companions(首选搭配) /
allowed_companions(允许搭配) / companions(明细含 count/rate/confidence)。

用法::

  python3 scripts/extract_fila_sku_color_pairing.py \
      [--input data/tables/fila_sku_outfit_colors.csv] \
      [--output-dir backend/intent/dictionaries]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

_SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = _SCRIPT_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_INPUT = ROOT / "data" / "tables" / "fila_sku_outfit_colors.csv"
DEFAULT_OUTPUT_DIR = ROOT / "backend" / "intent" / "dictionaries"

# 与 extract_color_series_pairing_rules.py 保持一致
PRIMARY_RATE = 0.08
ALLOWED_RATE = 0.02

# 配对维度: (输出文件名, 锚点角色, 伴侣角色, 描述)
PAIR_DIMENSIONS = [
    (
        "fila_sku_color_pairing_top_bottom.yaml",
        "上装",
        "下装",
        "FILA SKU 搭配色系配对统计 (上装→下装)",
    ),
    (
        "fila_sku_color_pairing_bottom_top.yaml",
        "下装",
        "上装",
        "FILA SKU 搭配色系配对统计 (下装→上装)",
    ),
    (
        "fila_sku_color_pairing_bottom_shoe.yaml",
        "下装",
        "鞋",
        "FILA SKU 搭配色系配对统计 (下装→鞋)",
    ),
]


def load_rows(path: Path) -> list[dict]:
    """读取 CSV，utf-8-sig 去掉 BOM。"""
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _clean(value: str | None) -> str:
    return (value or "").strip()


def parse_color_map(row: dict) -> dict[str, str] | None:
    """解析单行 ``json`` 列，返回 {role: color_series}。

    跳过错误记录: ``error`` 非空、``json`` 为空或解析失败、非 dict。
    """
    if _clean(row.get("error")):
        return None
    raw = _clean(row.get("json"))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    cleaned = {_clean(k): _clean(v) for k, v in data.items()}
    if not any(cleaned.values()):
        return None
    return cleaned


def confidence(rate: float) -> str:
    if rate >= 0.20:
        return "high"
    if rate >= PRIMARY_RATE:
        return "medium"
    if rate >= ALLOWED_RATE:
        return "low"
    return "rare"


def build_dimension(
    rows: list[dict],
    anchor_role: str,
    companion_role: str,
) -> tuple[dict, int, int, int]:
    """归纳单一配对维度的规则 (格式同 color_series_pairing.yaml)。

    Returns:
        (pairing_rules, total_records, analyzed_records, skipped_records)
    """
    anchor_count: Counter[str] = Counter()
    companion_counter: dict[str, Counter[str]] = defaultdict(Counter)
    analyzed = 0
    skipped = 0

    for row in rows:
        colors = parse_color_map(row)
        if colors is None:
            skipped += 1
            continue
        anchor = colors.get(anchor_role)
        companion = colors.get(companion_role)
        if not anchor or not companion:
            # 该行缺少此维度所需的角色，不计入此维度但不算错误
            continue
        analyzed += 1
        anchor_count[anchor] += 1
        companion_counter[anchor][companion] += 1

    all_anchors = sorted(anchor_count.keys())
    pairing_rules: dict[str, dict] = {}
    for anchor in all_anchors:
        total = int(anchor_count[anchor])
        comp_counter = companion_counter.get(anchor, Counter())
        primary: list[str] = []
        allowed: list[str] = []
        companions: list[dict] = []
        for comp, count in comp_counter.most_common():
            if comp == anchor:
                continue
            count = int(count)
            rate = (count / total) if total else 0.0
            if rate >= ALLOWED_RATE:
                allowed.append(comp)
            if rate >= PRIMARY_RATE:
                primary.append(comp)
            companions.append({
                "color_series": comp,
                "count": count,
                "rate": round(rate, 4),
                "confidence": confidence(rate),
            })

        pairing_rules[anchor] = {
            "anchor_count": total,
            "primary_companions": primary,
            "allowed_companions": allowed,
            "companions": companions,
        }

    total_records = len(rows)
    return pairing_rules, total_records, analyzed, skipped


def build_payload(
    rows: list[dict],
    anchor_role: str,
    companion_role: str,
    description: str,
    source: str,
) -> tuple[dict, int, int, int]:
    pairing_rules, total, analyzed, skipped = build_dimension(
        rows, anchor_role, companion_role
    )
    payload = {
        "meta": {
            "description": description,
            "source": source,
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "total_records": total,
            "analyzed_records": analyzed,
            "skipped_records": skipped,
            "thresholds": {
                "primary_rate": PRIMARY_RATE,
                "allowed_rate": ALLOWED_RATE,
            },
        },
        "pairing_rules": pairing_rules,
    }
    return payload, total, analyzed, skipped


def _write_header(f, payload: dict, anchor_role: str, companion_role: str) -> None:
    meta = payload["meta"]
    f.write(f"# {meta['description']}\n")
    f.write(f"# 数据源: {meta['source']}\n")
    f.write(
        "# 字段: anchor_count(锚点统计数) / primary_companions(首选搭配) / "
        "allowed_companions(允许搭配) / companions(明细)\n"
    )
    f.write(
        f"# 锚点角色: {anchor_role} / 伴侣角色: {companion_role}; "
        f"共 {len(payload['pairing_rules'])} 个 {anchor_role} 色系, "
        f"{meta['analyzed_records']}/{meta['total_records']} 条有效记录 "
        f"(跳过 {meta['skipped_records']} 条错误记录)\n"
    )
    f.write("# 生成脚本: scripts/extract_fila_sku_color_pairing.py\n\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract FILA SKU color-series pairing rules from CSV",
    )
    parser.add_argument(
        "--input", type=Path, default=DEFAULT_INPUT,
        help="Path to fila_sku_outfit_colors.csv",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
        help="Directory for output YAML files",
    )
    args = parser.parse_args()

    rows = load_rows(args.input)
    source = f"data/tables/{args.input.name}"
    args.output_dir.mkdir(parents=True, exist_ok=True)

    for filename, anchor_role, companion_role, desc in PAIR_DIMENSIONS:
        payload, total, analyzed, skipped = build_payload(
            rows, anchor_role, companion_role, desc, source,
        )
        out_path = args.output_dir / filename
        with out_path.open("w", encoding="utf-8") as f:
            _write_header(f, payload, anchor_role, companion_role)
            yaml.dump(
                payload, f,
                allow_unicode=True, sort_keys=True, default_flow_style=False,
                width=100,
            )
        print(
            f"Wrote {out_path}\n"
            f"  {anchor_role}→{companion_role}: "
            f"{analyzed}/{total} records, "
            f"{len(payload['pairing_rules'])} anchors "
            f"(skipped {skipped})"
        )


if __name__ == "__main__":
    main()
