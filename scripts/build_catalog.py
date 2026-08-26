#!/usr/bin/env python3
"""FILA：从 data/tables 构建 skus.jsonl 与 spu_to_skus.json。

与 descente build_catalog 差异：
  - 货号来源：product_master 在售款（onsell∈{1,2}）× product_attr 颜色货号
  - 严格过滤：onsell∈{1,2} 且 up_time >= 2023-01-01，两字段均不能为空
  - 不再补充搭配引用的 SKU（曾绕过 up_time 过滤引入空 up_time/onsell=None 脏数据）
  - 品牌/检索文案为 FILA；可选 v2 xlsx / fila_products_brief 补 role、价格、场景
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.etl_common import (
    EtlLogger,
    ProductTables,
    is_onsell,
    processed_dir,
    product_dir_path,
    reports_dir,
    text_or_empty,
)

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from index_sync_state import (
    DEFAULT_STATE_PATH,
    load_state,
    save_state,
)


def _progress(msg: str) -> None:
    """进度写到 stderr，避免与最终统计 stdout 混用。"""
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


# 类目白名单：仅保留服装/鞋类/配件（真正的搭配单品）。
# 其余 cat_l1（广宣用品/礼品/装备/雪具/福袋/其它 + CRM 促销品被错填成产品名
# 的脏值，如「CRM熊猫包挂」「上海迪士尼门票」等）一律丢弃——既非穿搭单品，
# 又会因 title 含「外套/裤」等词被 infer_up_down_from_title 误染 role。
_ALLOWED_CAT_L1: frozenset[str] = frozenset({"服装", "鞋类", "配件"})

# 品牌=FILA 过滤：product_master.id_brand 是安踏集团全品牌货池的数字 ID，
# data/tables 里混入了 ANTA/DESCENTE/KOLON/Sprandi/Salomon 等同集团其它品牌。
# 仅保留 FILA 系。ID 取自权威源 fila_products_brief_prod.xlsx 反查 master.id_brand
# 的并集（采样确认），非凭标题猜测：
#   1  = FILA 主品牌        (款号 F1/A1 前缀)
#   17 = FILA KIDS 童装     (款号 K1 前缀)
#   21 = FILA FUSION 子品牌
#   10 = 福袋池-FILA × MIHARA 联名
# 需收紧到仅主品牌时，删去对应 ID 即可。
_FILA_BRAND_IDS: frozenset[str] = frozenset({"1", "17", "21", "10"})


# 粗类桶 → 细类归一化
# 上游 catalog 对部分 SKU 打了带"类"后缀的粗分桶或泛词（包类/帽类/袜类/短T类/
# 短裤类/内衣类/配件内衣类/针织类/内搭），比真正的 category_l2 粗，且与同名细类
# 并存会污染搭配召回与笛卡尔积打分。此处按"粗类→细类"在构建期修正：
#   - 无歧义的查静态表（包类→包、帽类→帽子、袜类→袜子、短T类→短袖T恤）；
#   - 歧义的（同名细类不止一个）用标题子串兜底（标题通常已含细类名，如"梭织短裤"
#     "运动内衣""长袖T"），按规则列表首命中即用；
#   - 仍匹配不到的保持原值，交由 non_clothing_exclusion / pairing 黑名单处理。
# 真正的 category_l2 均为具体品类名（梭织长裤、短袖T恤、连衣裙…），不会落入此表。
_COARSE_L2_STATIC_MAP: dict[str, str] = {
    "包类": "包",
    "帽类": "帽子",
    "袜类": "袜子",
    "短T类": "短袖T恤",
}
# 歧义粗类 → [(标题关键词, 目标细类)] 有序列表，按顺序首命中即用（长词优先避免误匹配）
_COARSE_L2_TITLE_RULES: dict[str, list[tuple[str, str]]] = {
    "短裤类": [
        ("针织五分裤", "针织五分裤"),
        ("梭织五分裤", "梭织五分裤"),
        ("针织七分裤", "针织七分裤"),
        ("梭织七分裤", "梭织七分裤"),
        ("针织短裤", "针织短裤"),
        ("梭织短裤", "梭织短裤"),
    ],
    "内衣类": [("运动内衣", "运动内衣")],
    "配件内衣类": [("内裤", "内裤")],
    "针织类": [("围巾", "围巾"), ("手套", "手套"), ("针织帽", "针织帽")],
    "内搭": [
        ("长袖T", "长袖T恤"),
        ("套头卫衣", "套头卫衣"),
        ("连帽卫衣", "连帽卫衣"),
        ("背心", "背心"),
    ],
}
_COARSE_L2_BUCKETS: frozenset[str] = frozenset(_COARSE_L2_STATIC_MAP) | frozenset(
    _COARSE_L2_TITLE_RULES
)


def _resolve_coarse_category_l2(rec: dict) -> str:
    """粗类桶 → 细类归一化（无歧义查表，歧义用标题子串兜底）。非粗类原样返回。"""
    cat = (rec.get("category_l2") or "").strip()
    if cat not in _COARSE_L2_BUCKETS:
        return cat
    if cat in _COARSE_L2_STATIC_MAP:
        return _COARSE_L2_STATIC_MAP[cat]
    title = rec.get("title") or ""
    for kw, fine in _COARSE_L2_TITLE_RULES.get(cat, ()):
        if kw in title:
            return fine
    return cat  # 兜底：保持原值，交由 exclusion/blacklist 处理


def main() -> None:
    parser = argparse.ArgumentParser(description="Build FILA skus.jsonl")
    parser.add_argument(
        "--product-dir",
        type=Path,
        default=None,
        help="商品 CSV 目录，默认 config paths.product_dir",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help=(
            "增量模式：仅对 updated_at 发生变化的款号重建 JSONL 记录，"
            "其余复用上次 skus.jsonl 中的已有记录（需状态文件）"
        ),
    )
    parser.add_argument(
        "--state-file",
        type=str,
        default="",
        help=f"状态 JSON 路径（默认 {DEFAULT_STATE_PATH}）",
    )
    parser.add_argument(
        "--no-up-time-filter",
        action="store_true",
        help=(
            "关闭 up_time >= 2023-01-01 过滤，仅保留 onsell∈{1,2} 过滤。"
            "用于调试或全量重建（会引入老款/无上架时间的 SKU）"
        ),
    )
    args = parser.parse_args()
    prod = args.product_dir or product_dir_path()
    out_dir = processed_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    _progress(f"[start] product_dir={prod.resolve()}")
    _progress(f"[start] out_dir={out_dir.resolve()}")

    # ── Incremental state setup ─────────────────────────────────────────
    state_path = (
        Path(args.state_file).expanduser().resolve()
        if args.state_file.strip()
        else DEFAULT_STATE_PATH
    )
    state = load_state(state_path) if args.incremental else None
    last_sync_str = (state or {}).get("last_catalog_sync_at")
    last_sync_dt: datetime | None = None
    if last_sync_str:
        try:
            last_sync_dt = datetime.fromisoformat(last_sync_str)
        except Exception:
            last_sync_dt = None
    sync_now = datetime.now(timezone.utc).astimezone()

    log = EtlLogger("catalog")
    log.emit(
        "catalog_load_started",
        {"path": str(prod.resolve()), "incremental": args.incremental},
    )

    _progress("[load] product tables ...")
    t_load = time.monotonic()
    tables = ProductTables.load(prod)
    onsell_goods = len(tables.iter_onsell_goods_ids(skip_up_time=args.no_up_time_filter))
    onsell_all = sum(1 for row in tables.masters.values() if is_onsell(row.get("onsell")))
    _progress(
        f"[load] product tables done in {time.monotonic() - t_load:.1f}s "
        f"(onsell∈{{1,2}} goods={onsell_all:,}, "
        f"after up_time>=2023-01-01 & onsell∈{{1,2}} filter={onsell_goods:,}"
        + ("" if not args.no_up_time_filter else "  [up_time filter OFF]")
    )

    # ── Compute dirty goods for incremental mode ──────────────────────────
    dirty_goods_ids: set[int] = set()
    incremental_active = False  # True only when we have a valid previous sync
    reused_count = 0
    if args.incremental and last_sync_dt is not None:
        max_update_times = tables.compute_max_update_times()
        dirty_goods_ids = {
            gid for gid, dt in max_update_times.items() if dt > last_sync_dt
        }
        incremental_active = True
        _progress(
            f"[incremental] last_sync={last_sync_str}, "
            f"dirty_goods={len(dirty_goods_ids)}"
        )
    elif args.incremental:
        _progress("[incremental] no previous sync found, falling back to full rebuild")

    # ── Load previous skus.jsonl for reuse ─────────────────────────────────
    prev_skus: dict[str, dict] = {}
    if incremental_active:
        skus_path = out_dir / "skus.jsonl"
        if skus_path.is_file():
            with skus_path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    sid = rec.get("sku_id", "")
                    if sid:
                        prev_skus[sid] = rec
            _progress(f"[incremental] loaded {len(prev_skus):,} prev skus for reuse")

    _progress("[scan] collecting on-sale sku candidates ...")
    t_scan = time.monotonic()
    sku_id_set: set[str] = set(tables.iter_needed_sku_ids(skip_up_time=args.no_up_time_filter))

    # 品牌=FILA 过滤：按 goods_id 回查 product_master.id_brand，排除同集团其它品牌。
    before_brand = len(sku_id_set)
    sku_id_set = {
        sid for sid in sku_id_set
        if text_or_empty(
            tables.masters.get(tables.sku_id_to_gid.get(sid), {}).get("id_brand")
        ) in _FILA_BRAND_IDS
    }
    filtered_brand = before_brand - len(sku_id_set)
    _progress(
        f"[scan] {len(sku_id_set):,} sku candidates from onsell∈{{1,2}}+up_time>=2023-01-01 "
        f"({time.monotonic() - t_scan:.1f}s, "
        f"brand∉FILA dropped={filtered_brand:,})",
    )

    sku_ids = sorted(sku_id_set)
    _progress(f"[scan] total sku candidates: {len(sku_ids):,}")

    quality: list[str] = ["# FILA 目录构建报告", "", f"- run_id: `{log.run_id}`"]
    spu_to_skus: dict[str, list[str]] = defaultdict(list)
    skus_out: dict[str, dict] = {}
    role_unknown = 0
    unresolved = 0
    filtered_cat_l1 = 0
    coarse_resolved = 0

    total = len(sku_ids)
    report_every = max(1, total // 20)
    _progress(f"[build] processing {total:,} skus ...")
    t_build = time.monotonic()

    for i, sku_id in enumerate(sku_ids, start=1):
        # ── Incremental: reuse unchanged records ──────────────────────────
        if incremental_active and sku_id in prev_skus:
            prev_gid = prev_skus[sku_id].get("id_goods")
            if prev_gid is not None and int(prev_gid) not in dirty_goods_ids:
                rec = prev_skus[sku_id]
                # 类目白名单：仅保留 服装/鞋类/配件，其余丢弃
                if (rec.get("category_l1") or "").strip() not in _ALLOWED_CAT_L1:
                    filtered_cat_l1 += 1
                    if i % report_every == 0 or i == total:
                        pct = 100.0 * i / total if total else 100.0
                        _progress(
                            f"[build] {i:,}/{total:,} ({pct:.0f}%) "
                            f"written={len(skus_out):,} unresolved={unresolved:,} "
                            f"reused={reused_count:,} filtered={filtered_cat_l1:,}",
                        )
                    continue
                # 粗类桶 → 细类归一化（增量复用的旧记录同样修正）
                resolved = _resolve_coarse_category_l2(rec)
                if resolved != (rec.get("category_l2") or "").strip():
                    rec["category_l2"] = resolved
                    coarse_resolved += 1
                skus_out[sku_id] = rec
                spu = str(rec.get("spu_id") or "")
                if spu:
                    spu_to_skus[spu].append(sku_id)
                reused_count += 1
                if i % report_every == 0 or i == total:
                    pct = 100.0 * i / total if total else 100.0
                    _progress(
                        f"[build] {i:,}/{total:,} ({pct:.0f}%) "
                        f"written={len(skus_out):,} unresolved={unresolved:,} "
                        f"reused={reused_count:,}",
                    )
                continue

        rec = tables.build_sku_record(sku_id)
        if not rec:
            unresolved += 1
            quality.append(f"- unresolved_sku: {sku_id}")
            continue
        # 类目白名单：仅保留 服装/鞋类/配件，其余（广宣/礼品/装备/雪具/福袋/CRM 等）丢弃
        if (rec.get("category_l1") or "").strip() not in _ALLOWED_CAT_L1:
            filtered_cat_l1 += 1
            continue
        # 粗类桶 → 细类归一化（包类→包、短T类→短袖T恤、短裤类按标题细分等）
        resolved = _resolve_coarse_category_l2(rec)
        if resolved != (rec.get("category_l2") or "").strip():
            rec["category_l2"] = resolved
            coarse_resolved += 1
        if rec.get("role") == "unknown":
            role_unknown += 1
        skus_out[sku_id] = rec
        spu = str(rec.get("spu_id") or "")
        if spu:
            spu_to_skus[spu].append(sku_id)

        if i % report_every == 0 or i == total:
            pct = 100.0 * i / total if total else 100.0
            _progress(
                f"[build] {i:,}/{total:,} ({pct:.0f}%) "
                f"written={len(skus_out):,} unresolved={unresolved:,} "
                f"reused={reused_count:,}",
            )

    build_elapsed = time.monotonic() - t_build
    rebuilt_count = len(skus_out) - reused_count
    _progress(
        f"[build] done in {build_elapsed:.1f}s: "
        f"skus={len(skus_out):,} unresolved={unresolved:,} "
        f"role_unknown={role_unknown:,} spus={len(spu_to_skus):,} "
        f"reused={reused_count:,} rebuilt={rebuilt_count:,} "
        f"filtered_cat_l1={filtered_cat_l1:,} coarse_resolved={coarse_resolved:,}",
    )

    # ── Attribute coverage report ─────────────────────────────────────────
    _COVERAGE_FIELDS = [
        ("性别",   "gender",       "list"),
        ("年龄",   "age",          "str"),
        ("上市",   "up_time",      "str"),
        # 上下装轴对鞋/配件不适用（恒为 鞋/配饰 或空），作覆盖率指标会长期停在
        # ~89% 制造假警报；role 才是对全部 SKU 有意义的轴，故改报 role 覆盖率。
        ("角色",   "role",         "role"),
        ("大类",   "category_l1",  "str"),
        ("中类",   "category_l2",  "str"),
        ("季节",   "season",       "list"),
        ("色名",   "color_name",   "str"),
        ("色系",   "color_series", "list"),
        ("风格",   "style_tags",   "list"),
        ("场景",   "occasion_tags","list"),
        ("层次",   "layer",        "str"),
        ("覆盖",   "coverage",     "str"),
        ("价格",   "price",        "price"),
        # 版型（modeling）仅对服装有意义：鞋类是「楦型」（另一概念，源列恒空）、
        # 配件不适用（恒空）、unknown 是 role 推断失败。以全量为分母会长期停在
        # ~52% 制造假警报；改报「服装池」覆盖率，分母 = role∈{top,bottoms,dress}。
        # 与 length_class/scene_domain 同构。
        ("版型",   "modeling",     "modeling"),
        # length_class 仅对 top/bottoms 有意义（鞋/配饰/连衣裙/泳装恒 n/a），
        # 以全量 SKU 为分母会长期封顶在 ~53% 制造假警报；改报「适用池」覆盖率，
        # 分母 = role∈{top,bottoms} 的 SKU 数。与 role 轴同理。
        ("长短",   "length_class", "length"),
        ("贴身",   "is_intimate",  "bool"),
        # scene_domain 对配件恒为 ""（中性跨场景复用，非未知），以全量为分母会
        # 长期停在 ~82% 制造假警报；改报「非配件池」覆盖率，分母 = role≠accessory。
        ("场景域", "scene_domain", "scene"),
        # descent 复刻新增字段的覆盖率
        ("品牌线", "brand_line",   "str"),
        ("年度",   "year",         "str"),
        ("卖点",   "selling_point_label", "str"),
        ("功能",   "features",     "str"),
        ("技术",   "technology",   "str"),
        ("货号",   "goods_sn",     "str"),
        ("在售",   "onsell",       "onsell"),
        ("销量",   "sales",        "int"),
    ]
    total_skus = len(skus_out) or 1
    # length_class 适用池：仅上下装。
    applicable_length_skus = sum(
        1 for rec in skus_out.values()
        if (rec.get("role") or "").strip().lower() in ("top", "bottoms")
    ) or 1
    # scene_domain 适用池：非配件（配件恒中性 ""，非「未覆盖」）。
    applicable_scene_skus = sum(
        1 for rec in skus_out.values()
        if (rec.get("role") or "").strip().lower() != "accessory"
    ) or 1
    # modeling 适用池：仅服装（top/bottoms/dress）。鞋类=楦型（另一概念，恒空）、
    # 配件不适用、unknown 为 role 推断失败——均不计入分母。
    applicable_modeling_skus = sum(
        1 for rec in skus_out.values()
        if (rec.get("role") or "").strip().lower() in ("top", "bottoms", "dress")
    ) or 1
    _progress("\n[coverage] ══════════════════════════════════════")
    _progress(f"[coverage] 属性覆盖率  (total skus={len(skus_out):,})")
    _progress("[coverage] ──────────────────────────────────────")
    for label, key, kind in _COVERAGE_FIELDS:
        hit = 0
        for rec in skus_out.values():
            val = rec.get(key)
            if kind == "list":
                if val and isinstance(val, list) and any(
                    str(v).strip() and str(v).strip().lower() != "n/a"
                    for v in val
                ):
                    hit += 1
            elif kind == "bool":
                if val is True:
                    hit += 1
            elif kind == "role":
                # role 总有值；真正"未覆盖"是 unknown（推断失败的兜底）。
                stripped = str(val).strip().lower() if val is not None else ""
                if stripped and stripped not in ("n/a", "unknown"):
                    hit += 1
            elif kind == "length":
                # 仅 long/short 算命中；n/a（含不适用的鞋/配饰/连衣裙/泳装）不计入
                # 分母 applicable_length_skus。
                stripped = str(val).strip().lower() if val is not None else ""
                if stripped in ("long", "short"):
                    hit += 1
            elif kind == "modeling":
                # 命中 = 归一到枚举（宽松/基础/舒适/修身/紧身/超宽松/ACTIVE）；
                # "" 出现在鞋类（楦型，另一概念）/配件（不适用）/unknown（role 失败），
                # 均不计入服装分母 applicable_modeling_skus。
                stripped = str(val).strip().lower() if val is not None else ""
                if stripped and stripped != "n/a":
                    hit += 1
            elif kind == "price":
                # price 为 float；0.0 表示源数据缺失（shop_price/price/v2 均无）
                try:
                    if val is not None and float(val) > 0:
                        hit += 1
                except (TypeError, ValueError):
                    pass
            elif kind == "scene":
                # 命中 = 任一具体域（daily/golf/tennis/…）；"" 仅出现在配件（中性，
                # 非未知），不计入非配件分母 applicable_scene_skus。
                stripped = str(val).strip().lower() if val is not None else ""
                if stripped and stripped != "n/a":
                    hit += 1
            elif kind == "onsell":
                # onsell 为 int；在售 = 1 或 2
                if val in (1, 2, "1", "2"):
                    hit += 1
            elif kind == "int":
                try:
                    if val is not None and int(val) > 0:
                        hit += 1
                except (TypeError, ValueError):
                    pass
            else:
                stripped = str(val).strip() if val is not None else ""
                if stripped and stripped.lower() != "n/a":
                    hit += 1
        if kind == "length":
            denom = applicable_length_skus
            pct = 100.0 * hit / denom
            _progress(
                f"[coverage]   {label:<6s} ({key:<16s}): {hit:>6,}/{denom:,}  ({pct:5.1f}%)  [适用池 top+bottoms]"
            )
        elif kind == "scene":
            denom = applicable_scene_skus
            pct = 100.0 * hit / denom
            _progress(
                f"[coverage]   {label:<6s} ({key:<16s}): {hit:>6,}/{denom:,}  ({pct:5.1f}%)  [非配件池]"
            )
        elif kind == "modeling":
            denom = applicable_modeling_skus
            pct = 100.0 * hit / denom
            _progress(
                f"[coverage]   {label:<6s} ({key:<16s}): {hit:>6,}/{denom:,}  ({pct:5.1f}%)  [服装池 top+bottoms+dress]"
            )
        else:
            pct = 100.0 * hit / total_skus
            _progress(f"[coverage]   {label:<6s} ({key:<16s}): {hit:>6,}/{len(skus_out):,}  ({pct:5.1f}%)")
    _progress("[coverage] ══════════════════════════════════════\n")

    skus_path = out_dir / "skus.jsonl"
    _progress(f"[write] {skus_path} ...")
    t_write = time.monotonic()
    with skus_path.open("w", encoding="utf-8") as handle:
        for sid in sorted(skus_out):
            handle.write(
                json.dumps(skus_out[sid], ensure_ascii=False) + "\n",
            )
    spu_path = out_dir / "spu_to_skus.json"
    with spu_path.open("w", encoding="utf-8") as handle:
        json.dump(dict(spu_to_skus), handle, ensure_ascii=False, indent=2)
    _progress(
        f"[write] done in {time.monotonic() - t_write:.1f}s "
        f"({skus_path.name}, {spu_path.name})",
    )

    # ── Update state on successful incremental run ─────────────────────────
    if args.incremental and state is not None:
        state["last_catalog_sync_at"] = sync_now.isoformat()
        save_state(state, state_path)
        _progress(
            f"[incremental] state updated: last_catalog_sync_at={sync_now.isoformat()}"
        )

    quality.extend(
        [
            f"- sku 行数: {len(skus_out)}",
            f"- role 未识别约计: {role_unknown}",
            f"- 增量复用: {reused_count}",
            f"- 增量重建: {rebuilt_count}",
            f"- 类目白名单丢弃(cat_l1∉服装/鞋类/配件): {filtered_cat_l1}",
            f"- 品牌非FILA丢弃(id_brand∉{{1,21,10}}): {filtered_brand}",
            f"- 粗类桶→细类归一化(包类/短T类/短裤类等): {coarse_resolved}",
        ],
    )
    (reports_dir() / "catalog_quality_report.md").write_text(
        "\n".join(quality) + "\n",
        encoding="utf-8",
    )
    log.emit(
        "catalog_summary",
        {
            "skus": len(skus_out),
            "role_unknown": role_unknown,
            "reused": reused_count,
            "rebuilt": rebuilt_count,
            "filtered_cat_l1": filtered_cat_l1,
            "filtered_brand": filtered_brand,
            "coarse_resolved": coarse_resolved,
            "incremental": args.incremental,
        },
    )
    log.close()
    print(
        f"Wrote {len(skus_out)} skus to {skus_path}\n"
        f"  unresolved: {unresolved}\n"
        f"  role_unknown: {role_unknown}\n"
        f"  spus: {len(spu_to_skus)}\n"
        f"  reused: {reused_count}\n"
        f"  rebuilt: {rebuilt_count}\n"
        f"  filtered_cat_l1: {filtered_cat_l1}\n"
        f"  filtered_brand: {filtered_brand}",
    )


if __name__ == "__main__":
    main()
