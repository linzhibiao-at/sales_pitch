#!/usr/bin/env python3
"""从搭配数据归纳色系(color_series)搭配规则，输出 YAML。

用法::

  python3 scripts/extract_color_series_pairing_rules.py [--input ...] [--output ...]

"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

import yaml

_SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = _SCRIPT_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._project_paths import load_paths

_PATHS = load_paths()
OUTFITS_PATH = _PATHS["outfits_json"]
DEFAULT_OUTPUT = (
    ROOT / "backend" / "intent" / "dictionaries" / "color_series_pairing.yaml"
)

PRIMARY_RATE = 0.08
ALLOWED_RATE = 0.02


def load_outfits(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _item_color_series(item: dict) -> list[str]:
    """从 outfit item 提取色系列表（多色名可能返回多个色系）。"""
    from backend.intent.color_series_mapper import map_color_to_series_list

    color = item.get("color") or {}
    name = str(color.get("attrName") or color.get("colorName") or "").strip()
    if not name:
        return []
    return map_color_to_series_list(name)


def confidence(rate: float) -> str:
    if rate >= 0.20:
        return "high"
    if rate >= PRIMARY_RATE:
        return "medium"
    if rate >= ALLOWED_RATE:
        return "low"
    return "rare"


def build_rules(outfits: list[dict]) -> dict:
    co_occurrence: dict[str, Counter[str]] = defaultdict(Counter)
    series_count: Counter[str] = Counter()
    analyzed_outfits = 0

    for outfit in outfits:
        items = outfit.get("items") or []
        series_in_outfit: list[str] = []
        for item in items:
            for cs in _item_color_series(item):
                if cs and cs not in series_in_outfit:
                    series_in_outfit.append(cs)

        unique_series = sorted(set(series_in_outfit))
        if len(unique_series) < 2:
            continue
        analyzed_outfits += 1

        for s in unique_series:
            series_count[s] += 1

        for a, b in combinations(unique_series, 2):
            co_occurrence[a][b] += 1
            co_occurrence[b][a] += 1

    all_series = sorted(series_count.keys())

    pairing_rules = {}
    for anchor in all_series:
        anchor_total = int(series_count[anchor])
        comp_counter = co_occurrence.get(anchor, Counter())
        companions = []
        primary: list[str] = []
        allowed: list[str] = []

        for comp in all_series:
            if comp == anchor:
                continue
            count = int(comp_counter.get(comp, 0))
            rate = (count / anchor_total) if anchor_total else 0.0
            if count == 0:
                continue
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

        companions.sort(key=lambda x: x["rate"], reverse=True)

        pairing_rules[anchor] = {
            "anchor_count": anchor_total,
            "primary_companions": primary,
            "allowed_companions": allowed,
            "companions": companions,
        }

    return {
        "meta": {
            "description": "FILA搭配数据归纳的色系(color_series)搭配规则",
            "source": "data/preview/fila_outfits.json",
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "total_outfits": len(outfits),
            "analyzed_outfits": analyzed_outfits,
            "thresholds": {
                "primary_rate": PRIMARY_RATE,
                "allowed_rate": ALLOWED_RATE,
            },
        },
        "pairing_rules": pairing_rules,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract color series pairing rules from FILA outfits",
    )
    parser.add_argument(
        "--input", type=Path, default=OUTFITS_PATH,
        help="Path to fila_outfits.json",
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help="Output YAML path",
    )
    args = parser.parse_args()

    outfits = load_outfits(args.input)
    rules = build_rules(outfits)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        yaml.dump(
            rules, f,
            allow_unicode=True, sort_keys=False, default_flow_style=False,
            width=100,
        )

    meta = rules["meta"]
    n_rules = len(rules["pairing_rules"])
    print(
        f"Wrote color series pairing rules to {args.output} "
        f"({meta['analyzed_outfits']}/{meta['total_outfits']} outfits analyzed, "
        f"{n_rules} color series)"
    )


if __name__ == "__main__":
    main()
