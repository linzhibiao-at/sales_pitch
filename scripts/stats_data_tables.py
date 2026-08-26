"""生成 data/tables 数据统计报告。

输出: docs/data_tables_stats.md
"""
from __future__ import annotations

import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.intent.color_series_mapper import map_color_to_series  # noqa: E402

TABLES = ROOT / "data" / "tables"
OUT = ROOT / "docs" / "data_tables_stats.md"


def fmt_pct(n: int, total: int) -> str:
    return f"{(n / total * 100):.2f}%" if total else "0.00%"


def quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


# ---------- 1. 颜色名 + 色系 (来自 product_sku.csv) ----------
def analyze_color() -> tuple[Counter, Counter, int, int]:
    color_name_counter: Counter = Counter()
    color_series_counter: Counter = Counter()
    total_rows = 0
    empty_color = 0
    sku_path = TABLES / "product_sku.csv"
    with sku_path.open(encoding="utf-8-sig") as f:
        r = csv.DictReader(f)
        for row in r:
            total_rows += 1
            attr = row.get("attr_name", "") or ""
            color = ""
            if "颜色:" in attr:
                # 形如 "颜色:苯胺粉;尺码:XL"
                color = attr.split("颜色:", 1)[1].split(";", 1)[0].strip()
            if not color:
                empty_color += 1
                continue
            color_name_counter[color] += 1
            series = map_color_to_series(color) or "未匹配"
            color_series_counter[series] += 1
    return color_name_counter, color_series_counter, total_rows, empty_color


# ---------- 2. market_price 分位数 (按商品大类) ----------
# 大类口径: cat_type 清洗后的主品类（服装/鞋/配饰/装备/其他）
MAJOR_BUCKET_KEYWORDS = [
    ("服装", ["服装", "服", "男代同款服", "鞋服"]),
    ("鞋", ["鞋", "鞋品", "鞋类", "鞋履", "鞋类(通用)"]),
    ("配饰", ["配件", "配饰", "帽", "袜", "包类", "包", "手套", "围巾",
              "团队球类", "球类", "伞类"]),
    ("装备", ["装备", "雪具", "高尔夫", "运动器材"]),
]


def bucket_major(cat_type: str, cat_alias: str) -> str:
    raw = (cat_type or "").strip()
    alias = (cat_alias or "").strip()
    text = raw
    if not text or text in {"其它", "其他", "内部"}:
        text = alias
    for label, kws in MAJOR_BUCKET_KEYWORDS:
        for kw in kws:
            if kw in text:
                return label
    # 礼盒 / CRM / 福袋 / 票务等归到 "其他"
    if any(k in text for k in ("CRM", "礼盒", "福袋", "票", "门票", "赠品",
                                "礼包", "奖品", "盲盒", "配饰及装备")):
        return "其他"
    return "其他"


def analyze_market_price() -> tuple[dict[str, list[float]], dict[str, int]]:
    # id_goods -> (cat_type, cat_alias) from ext
    id_goods_to_cat: dict[str, tuple[str, str]] = {}
    with (TABLES / "product_master_ext.csv").open(encoding="utf-8-sig") as f:
        r = csv.DictReader(f)
        for row in r:
            ig = row.get("id_goods", "")
            if not ig:
                continue
            id_goods_to_cat[ig] = (row.get("cat_type", ""), row.get("cat_alias", ""))

    by_bucket: dict[str, list[float]] = defaultdict(list)
    bucket_count: Counter = Counter()
    no_cat = 0
    with (TABLES / "product_master.csv").open(encoding="utf-8-sig") as f:
        r = csv.DictReader(f)
        for row in r:
            ig = row.get("id_goods", "")
            try:
                price = float(row.get("market_price") or 0)
            except ValueError:
                continue
            if price <= 0:
                continue
            cat_info = id_goods_to_cat.get(ig)
            if not cat_info:
                no_cat += 1
                continue
            bucket = bucket_major(*cat_info)
            by_bucket[bucket].append(price)
            bucket_count[bucket] += 1
    return by_bucket, bucket_count


# ---------- 3. 中类名 (cat_alias) 分布 ----------
def analyze_middle_class() -> Counter:
    c: Counter = Counter()
    with (TABLES / "product_master_ext.csv").open(encoding="utf-8-sig") as f:
        r = csv.DictReader(f)
        for row in r:
            v = (row.get("cat_alias") or "").strip()
            if v:
                c[v] += 1
    return c


# ---------- 4. 版型 (modeling) 分布 ----------
VALID_FIT = {"修身", "基础", "宽松", "标准", "紧身", "舒适", "超宽松"}


def analyze_fit() -> Counter:
    c: Counter = Counter()
    with (TABLES / "product_master_ext.csv").open(encoding="utf-8-sig") as f:
        r = csv.DictReader(f)
        for row in r:
            v = (row.get("modeling") or "").strip()
            if not v:
                continue
            # 拆分复合值（如 "基础/宽松/修身/..."）
            parts = [p.strip() for p in v.replace("，", "/").split("/") if p.strip()]
            matched = False
            for p in parts:
                # 取首段（避免 "TOUR（场上修身版型）" 这类）
                for fit in VALID_FIT:
                    if fit in p:
                        c[fit] += 1
                        matched = True
                        break
            if not matched:
                c[f"其他:{v}"] += 1
    return c


# ---------- 渲染 markdown ----------
def render(
    color_names: Counter,
    color_series: Counter,
    color_total: int,
    color_empty: int,
    price_by_bucket: dict[str, list[float]],
    price_counts: dict[str, int],
    middle_class: Counter,
    fit: Counter,
) -> str:
    lines: list[str] = []
    lines.append("# data/tables 数据统计报告\n")
    lines.append(f"生成日期: 2026-07-07\n")
    lines.append("数据源: `data/tables/` 目录\n\n---\n")

    # 1. 颜色
    lines.append("## 1. 颜色名 & 色系分布\n")
    lines.append(f"数据源: `product_sku.csv` (SKU 级)\n")
    lines.append(
        f"- SKU 总行数: **{color_total:,}**\n"
        f"- 解析到颜色名的 SKU: **{sum(color_names.values()):,}** "
        f"({fmt_pct(sum(color_names.values()), color_total)})\n"
        f"- 颜色为空的 SKU: **{color_empty:,}**\n"
        f"- 颜色名唯一值数量: **{len(color_names):,}**\n\n"
    )

    lines.append("### 1.1 色系分布\n")
    lines.append("| 色系 | SKU 数 | 占比 |")
    lines.append("|---|---:|---:|")
    series_total = sum(color_series.values())
    for s, n in color_series.most_common():
        lines.append(f"| {s} | {n:,} | {fmt_pct(n, series_total)} |")
    lines.append("")

    lines.append("### 1.2 颜色名 Top 50\n")
    lines.append("| 颜色名 | SKU 数 | 占比 |")
    lines.append("|---|---:|---:|")
    name_total = sum(color_names.values())
    for name, n in color_names.most_common(50):
        lines.append(f"| {name} | {n:,} | {fmt_pct(n, name_total)} |")
    lines.append("\n---\n")

    # 2. market_price 分位数
    lines.append("## 2. 按商品大类的 market_price 分位数分布\n")
    lines.append("数据源: `product_master.csv` (商品级) ⨝ `product_master_ext.csv` (大类信息)\n")
    lines.append(
        '大类口径: 按 `cat_type`/`cat_alias` 清洗为 服装 / 鞋 / 配饰 / 装备 / 其他 '
        '(CRM 礼盒、票务、福袋等归入「其他」)\n\n'
    )
    lines.append("| 商品大类 | 商品数 | P25 | P50 (中位) | P75 | P90 | P95 | Min | Max |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    order = ["服装", "鞋", "配饰", "装备", "其他"]
    for bucket in order:
        prices = sorted(price_by_bucket.get(bucket, []))
        if not prices:
            lines.append(f"| {bucket} | 0 | - | - | - | - | - | - | - |")
            continue
        lines.append(
            f"| {bucket} | {len(prices):,} | "
            f"{quantile(prices, 0.25):.0f} | "
            f"{quantile(prices, 0.50):.0f} | "
            f"{quantile(prices, 0.75):.0f} | "
            f"{quantile(prices, 0.90):.0f} | "
            f"{quantile(prices, 0.95):.0f} | "
            f"{prices[0]:.0f} | "
            f"{prices[-1]:.0f} |"
        )
    lines.append("\n---\n")

    # 3. 中类名
    lines.append("## 3. 中类名分布\n")
    lines.append("数据源: `product_master_ext.csv` 字段 `cat_alias`（755 个唯一值）\n")
    lines.append(f"- 中类名唯一值数: **{len(middle_class):,}**\n")
    lines.append(f"- 有中类名商品数: **{sum(middle_class.values()):,}**\n\n")
    lines.append("### 3.1 Top 40 中类名\n")
    lines.append("| 中类名 | 商品数 | 占比 |")
    lines.append("|---|---:|---:|")
    mc_total = sum(middle_class.values())
    for name, n in middle_class.most_common(40):
        lines.append(f"| {name} | {n:,} | {fmt_pct(n, mc_total)} |")
    lines.append("\n---\n")

    # 4. 版型
    lines.append("## 4. 版型分布\n")
    lines.append("数据源: `product_master_ext.csv` 字段 `modeling`\n")
    lines.append(
        f"- 版型唯一值数 (清洗后): **{len(fit):,}**\n"
        f"- 有版型信息商品数: **{sum(fit.values()):,}**\n\n"
    )
    lines.append("### 4.1 版型分布\n")
    lines.append("| 版型 | 商品数 | 占比 |")
    lines.append("|---|---:|---:|")
    fit_total = sum(fit.values())
    for name, n in fit.most_common():
        lines.append(f"| {name} | {n:,} | {fmt_pct(n, fit_total)} |")
    lines.append("")

    return "\n".join(lines) + "\n"


def main() -> int:
    print("Analyzing color names & series from product_sku.csv (8M+ rows)...", flush=True)
    color_names, color_series, color_total, color_empty = analyze_color()
    print(f"  -> {len(color_names):,} unique color names", flush=True)

    print("Analyzing market_price quantiles by major category...", flush=True)
    price_by_bucket, price_counts = analyze_market_price()
    print(f"  -> buckets: {dict(price_counts)}", flush=True)

    print("Analyzing middle class (cat_alias)...", flush=True)
    middle_class = analyze_middle_class()

    print("Analyzing fit (modeling)...", flush=True)
    fit = analyze_fit()

    print(f"Writing report to {OUT}...", flush=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    md = render(
        color_names, color_series, color_total, color_empty,
        price_by_bucket, price_counts, middle_class, fit,
    )
    OUT.write_text(md, encoding="utf-8")
    print("Done.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
