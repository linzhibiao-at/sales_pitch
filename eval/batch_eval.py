"""批量评测：按中类+性别枚举近 120 天上市的 SKU，跑完整推荐pipeline，输出JSON结果。

结果按时间戳存储到 eval/results/{YYYYMMDDHH}/ 目录，多次运行不覆盖。

用法:
    cd fila_agent_html
    python -m eval.batch_eval

快速测试（只跑前 3 个 SKU）:
    python -m eval.batch_eval --limit 3

多线程并行（默认 CPU 核数 × 2）:
    python -m eval.batch_eval --workers 8

跳过 LLM 排序和理由（加快评测）:
    python -m eval.batch_eval --skip-llm-rank-reason

导出输入 SKU 与搭配推荐结果到 JSON:
    python -m eval.batch_eval --save-json=true
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tqdm import tqdm

# 让 backend 包可被导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import get_root
from backend.empty_image_urls import sku_has_empty_tryon_image
from backend.local_data_store import LocalDataStore
from backend.models import ChatRequest, normalize_season
from backend.retrieval.es_client import EsClient
from backend.services.recommend_service import RecommendService
from eval.batch_eval_outfit_es import index_batch_eval_outfits
from eval.defect_analyzer import OutfitDefectAnalyzer
from eval.aesthetic_analyzer import AestheticAnalyzer

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("batch_eval")
logger.setLevel(logging.INFO)


def _configure_verbose_logging() -> None:
    for name in logging.root.manager.loggerDict:
        logging.getLogger(name).setLevel(logging.INFO)
    logging.getLogger("elasticsearch").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

ALLOWED_GENDERS = {"男", "女", "男童", "女童"}
ALLOWED_UP_DOWN = {"上装", "下装", "鞋"}
SHOE_ROLE = "shoes"

# 中文 → 英文文件名映射
_UP_DOWN_EN = {"上装": "top", "下装": "bottom", "鞋": "shoes"}
_CAT_L2_EN = {
    "POLO衫": "polo",
    "T恤": "tshirt",
    "内搭打底": "base_layer",
    "卫衣": "hoodie",
    "夹克外套": "jacket",
    "棉服": "padded_coat",
    "滑雪服": "ski_jacket",
    "羽绒服": "down_jacket",
    "衬衫": "shirt",
    "裙装": "skirt",
    "训练服": "training_wear",
    "马夹": "vest",
    "滑雪裤": "ski_pants",
    "短裤": "shorts",
    "紧身裤": "leggings",
    "长裤": "trousers",
    # ── 鞋类中类 ─────────────────────────────────────────────────────
    "ORIGINALE老爹鞋": "originale_dad_shoes",
    "儿童复古跑鞋": "kids_retro_running",
    "儿童跑鞋": "kids_running",
    "儿童凉鞋": "kids_sandals",
    "EXPLORE户外鞋": "explore_outdoor",
    "FITNESS跑步鞋": "fitness_running",
    "MILANO板鞋": "milano_skate",
    "老爹鞋": "dad_shoes",
    "儿童户外鞋": "kids_outdoor",
    "MILANO休闲鞋": "milano_casual",
    "儿童闪灯鞋": "kids_lightup",
    "板鞋": "skate_shoes",
    "滑板生活鞋": "skate_lifestyle",
    "机能潮鞋": "tech_sneaker",
    "未来潮鞋": "future_sneaker",
    "薄底鞋": "thin_sole",
    "MILANO拖鞋": "milano_slippers",
    "户外鞋": "outdoor_shoes",
    "GOLF软钉高球鞋": "golf_soft_spike",
    "复古运动鞋": "retro_sneaker",
    "ORIGINALE凉鞋": "originale_sandals",
    "凉鞋": "sandals",
    "拖鞋": "slippers",
    "MILANO帆布鞋": "milano_canvas",
    "儿童经典板鞋": "kids_classic_skate",
    "厚底潮鞋": "platform_sneaker",
    "专业滑板鞋": "pro_skate",
    "面包板鞋": "bread_skate",
    "复古篮球鞋": "retro_basketball",
    "TENNIS网球生活鞋": "tennis_lifestyle",
    "复古潮鞋": "retro_trend_sneaker",
    "摩登运动鞋": "modern_sneaker",
    "经典板鞋": "classic_skate",
    "TENNIS性能网球鞋": "tennis_performance",
    "HERITAGE板鞋": "heritage_skate",
    "儿童场上篮球鞋": "kids_basketball",
    "儿童学步鞋": "kids_walker",
    "儿童网球鞋": "kids_tennis",
    "软钉高球鞋": "soft_spike_golf",
    "路跑鞋": "road_running",
    "ORIGINALE拖鞋": "originale_slippers",
    "复古板鞋": "retro_skate",
    "摩登板鞋": "modern_skate",
    "复古帆布鞋": "retro_canvas",
    "儿童高尔夫": "kids_golf",
    "健身房跑鞋": "gym_running",
    "HERITAGE老爹鞋": "heritage_dad_shoes",
    "厚底鞋": "platform_shoes",
    "跑鞋": "running_shoes",
}


def _normalize_up_down(row: dict[str, Any]) -> str:
    """归一化 up_down：鞋类 SKU 的 up_down_raw 多数为空，统一归为 "鞋"。"""
    role = row.get("role") or ""
    if role == SHOE_ROLE:
        return "鞋"
    return row.get("up_down_raw") or ""


def _parse_bool_arg(value: str) -> bool:
    normalized = (value or "").strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise argparse.ArgumentTypeError(
        f"invalid boolean value: {value!r} (use true or false)"
    )


def _build_outfit_rec_export(
    results: list[dict[str, Any]],
    *,
    eval_time: str,
    skip_llm_rank_reason: bool,
) -> dict[str, Any]:
    """组装输入 SKU 与搭配推荐结果的导出 JSON（精简版）。

    每条记录只保留 input_sku_id / input_sku_title 以及每套搭配的单品列表
    （单品仅含 sku_id / tryon_image / title）。
    """
    records: list[dict[str, Any]] = []
    for entry in results:
        input_sku = entry.get("input_sku") or {}
        record: dict[str, Any] = {
            "input_sku_id": entry.get("input_sku_id"),
            "input_sku_title": input_sku.get("title"),
            "outfits": entry.get("outfit_items") or [],
        }
        if entry.get("error"):
            record["error"] = entry["error"]
        records.append(record)
    return {
        "eval_time": eval_time,
        "skip_llm_rank_reason": skip_llm_rank_reason,
        "total_skus": len(records),
        "success_count": sum(1 for r in records if not r.get("error")),
        "error_count": sum(1 for r in records if r.get("error")),
        "records": records,
    }


def _make_filename(up_down: str, cat_l2: str) -> str:
    """生成英文文件名，如 top__polo.json。

    复合品类（如 "短裤/裙"）是合法数据，但其中的 "/" 会被 Path 当成目录
    分隔符导致写文件时 FileNotFoundError，这里统一把路径分隔符替换为 "_"。
    """
    ud = _UP_DOWN_EN.get(up_down, up_down)
    cat = _CAT_L2_EN.get(cat_l2, cat_l2)
    safe_ud = ud.replace("/", "_").replace("\\", "_")
    safe_cat = cat.replace("/", "_").replace("\\", "_")
    return f"{safe_ud}__{safe_cat}.json"


def _parse_up_time(raw: Any) -> datetime | None:
    """解析 SKU 的 up_time（ES date 字段，yyyy-MM-dd 或 yyyy-MM-dd HH:mm:ss）。

    非法/空值返回 None。去时区后返回 naive datetime 供与截止日比较。
    """
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).strip())
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def _is_recent_up_time(raw: Any, cutoff: datetime) -> bool:
    """up_time 距今在 cutoff 之后（近 120 天上市）；无上架时间视为不符合。"""
    dt = _parse_up_time(raw)
    if dt is None:
        return False
    return dt >= cutoff


def sample_skus(
    store: LocalDataStore,
) -> list[dict[str, Any]]:
    """按 (up_down, category_l2, gender) 分组，取全量近期上市 SKU（不再随机抽样）。

    up_down 取归一化值：上装/下装沿用 up_down_raw，鞋类（role=shoes）统一归为 "鞋"。
    season 归一化后必须包含 "夏"（春夏/常青等多季节也保留）。
    up_time 必须在近 120 天内（仅评测近期上市的 SKU）。
    组内按 sku_id 稳定排序，保证多次运行结果一致。
    """
    store.load()
    up_time_cutoff = datetime.now() - timedelta(days=120)
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in store.skus.values():
        seasons = normalize_season(row.get("season"))
        if "夏" not in seasons:
            continue
        if not _is_recent_up_time(row.get("up_time"), up_time_cutoff):
            continue
        up_down = _normalize_up_down(row)
        cat_l2 = row.get("category_l2") or ""
        raw_gender = row.get("gender")
        if isinstance(raw_gender, list):
            raw_gender = raw_gender[0] if raw_gender else ""
        gender = raw_gender or ""
        tryon = (row.get("tryon_image") or "").strip()
        if up_down not in ALLOWED_UP_DOWN:
            continue
        if gender not in ALLOWED_GENDERS:
            continue
        if not cat_l2 or not tryon:
            continue
        if sku_has_empty_tryon_image(row):
            continue
        groups[(up_down, cat_l2, gender)].append(row)

    sampled: list[dict[str, Any]] = []
    for key in sorted(groups.keys()):
        candidates = sorted(groups[key], key=lambda r: str(r.get("sku_id") or ""))
        sampled.extend(candidates)

    logger.info(
        "采样完成: %d 组, 共 %d 个 SKU",
        len(groups),
        len(sampled),
    )
    return sampled


async def run_pipeline_for_sku(
    svc: RecommendService,
    sku: dict[str, Any],
    *,
    skip_llm_rank_reason: bool = False,
) -> dict[str, Any]:
    """对单个 SKU 跑完整 chat_stream pipeline，收集结果。"""
    sku_id = sku["sku_id"]
    req = ChatRequest(
        message="",
        selected_sku_id=sku_id,
        enable_tryon=False,
        enable_llm_rank_reason=not skip_llm_rank_reason,
    )
    outfits: list[dict[str, Any]] = []
    intent_data: dict[str, Any] = {}
    intent_debug: dict[str, Any] = {}
    es_debug: list[dict[str, Any]] = []
    timings: dict[str, Any] = {}
    total_ms = 0

    async for ev in svc.chat_stream(req):
        ev_type = ev.get("type")
        if ev_type == "intent":
            intent_data = ev.get("intent") or {}
            intent_debug = {
                "method": ev.get("method"),
                "confidence": ev.get("confidence"),
                "llm_fallback": ev.get("llm_fallback"),
                "image_override": ev.get("image_override"),
                "image_override_slots": ev.get("image_override_slots"),
                "slots_detail": ev.get("slots_detail"),
            }
        elif ev_type == "es_debug":
            es_debug = ev.get("queries") or []
        elif ev_type == "outfit_results":
            outfits = ev.get("outfits") or []
        elif ev_type == "done":
            total_ms = ev.get("total_ms") or 0

    timings["total_ms"] = total_ms
    return {
        "outfits": outfits,
        "intent": intent_data,
        "intent_debug": intent_debug,
        "es_debug": es_debug,
        "timings": timings,
    }


def slim_outfits(outfits: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    outfit_ids: list[str] = []
    meta: list[dict[str, Any]] = []
    for outfit in outfits:
        oid = str(outfit.get("outfit_id") or "").strip()
        if not oid:
            continue
        outfit_ids.append(oid)
        row = {
            "outfit_id": oid,
            "recall_source": outfit.get("recall_source"),
            "rank_score": outfit.get("rank_score"),
            "rank_order": outfit.get("rank_order"),
            "rank_score_breakdown": outfit.get("rank_score_breakdown"),
            "reason": outfit.get("reason") or "",
            "display_image": outfit.get("display_image"),
            "outfit_tryon_image": outfit.get("outfit_tryon_image") or outfit.get("tryon_result_image"),
        }
        if outfit.get("is_synthetic") or oid.startswith("synth_"):
            row["snapshot"] = outfit
        meta.append(row)
    return list(dict.fromkeys(outfit_ids)), meta


def _slim_outfit_items(
    outfits: list[dict[str, Any]],
    eval_outfit_ids: list[str],
) -> list[dict[str, Any]]:
    """提取每套搭配的单品精简信息（sku_id / tryon_image / title）。"""
    slimmed: list[dict[str, Any]] = []
    for idx, outfit in enumerate(outfits):
        if not isinstance(outfit, dict):
            continue
        oid = (
            eval_outfit_ids[idx]
            if idx < len(eval_outfit_ids)
            else str(outfit.get("outfit_id") or "").strip()
        )
        items = [
            {
                "sku_id": str(it.get("sku_id") or "").strip(),
                "tryon_image": (it.get("tryon_image") or "").strip(),
                "title": it.get("title") or "",
            }
            for it in (outfit.get("items") or [])
            if isinstance(it, dict)
        ]
        slimmed.append({"outfit_id": oid, "items": items})
    return slimmed


def _process_single_sku(
    sku: dict[str, Any],
    store_skus: dict[str, dict[str, Any]],
    defect_analyzer: OutfitDefectAnalyzer | None,
    aesthetic_analyzer: AestheticAnalyzer | None,
    *,
    skip_llm_rank_reason: bool = False,
) -> dict[str, Any]:
    sku_id = sku["sku_id"]
    tryon_url = (sku.get("tryon_image") or "").strip()
    input_sku = {
        "title": sku.get("title"),
        "gender": sku.get("gender"),
        "category_l2": sku.get("category_l2"),
        "up_down": _normalize_up_down(sku),
        "role": sku.get("role"),
        "tryon_image": tryon_url,
        "price": sku.get("price"),
    }
    entry: dict[str, Any] = {
        "input_sku_id": sku_id,
        "input_sku": input_sku,
        "outfit_ids": [],
        "outfit_meta": [],
        "outfit_items": [],
        "outfit_es_index": {"success": 0, "error": 0},
        "intent": {},
        "intent_debug": {},
        "es_debug": [],
        "timings": {},
        "error": None,
    }
    try:
        svc = RecommendService()
        es = EsClient()
        result = asyncio.run(
            run_pipeline_for_sku(
                svc,
                sku,
                skip_llm_rank_reason=skip_llm_rank_reason,
            )
        )
        eval_outfit_ids, ok, err = index_batch_eval_outfits(
            es,
            result["outfits"],
            input_sku_id=str(sku_id),
            input_sku=input_sku,
        )
        _original_ids, outfit_meta = slim_outfits(result["outfits"])
        entry["outfit_ids"] = eval_outfit_ids
        for idx, meta in enumerate(outfit_meta):
            if idx >= len(eval_outfit_ids):
                break
            meta["original_outfit_id"] = meta["outfit_id"]
            meta["outfit_id"] = eval_outfit_ids[idx]
        entry["outfit_meta"] = outfit_meta
        entry["outfit_items"] = _slim_outfit_items(
            result["outfits"], eval_outfit_ids,
        )
        entry["outfit_es_index"] = {"success": ok, "error": err}
        entry["intent"] = result["intent"]
        entry["intent_debug"] = result["intent_debug"]
        entry["es_debug"] = result["es_debug"]
        entry["timings"] = result["timings"]

        if defect_analyzer:
            intent_dict = result.get("intent") or {}
            anchor_row = store_skus.get(str(sku_id))
            outfit_defects: list[dict[str, Any]] = []
            for outfit in result.get("outfits") or []:
                if not isinstance(outfit, dict):
                    continue
                defect_report = defect_analyzer.analyze(
                    outfit, intent_dict, anchor_row,
                )
                if defect_report.has_defects:
                    outfit_defects.append({
                        "outfit_id": defect_report.outfit_id,
                        "defect_count": defect_report.defect_count,
                        "max_severity": defect_report.max_severity,
                        "defects": [d.to_dict() for d in defect_report.defects],
                    })
            entry["defects"] = outfit_defects
            entry["defect_count"] = sum(
                d["defect_count"] for d in outfit_defects
            )

        if aesthetic_analyzer:
            intent_dict = result.get("intent") or {}
            outfit_aesthetics: list[dict[str, Any]] = []
            for outfit in result.get("outfits") or []:
                if not isinstance(outfit, dict):
                    continue
                aesthetic_report = aesthetic_analyzer.analyze(
                    outfit, intent_dict,
                )
                if aesthetic_report.is_valid:
                    outfit_aesthetics.append(aesthetic_report.to_dict())
            entry["aesthetics"] = outfit_aesthetics
            scores = [a["overall_score"] for a in outfit_aesthetics]
            entry["aesthetic_avg_score"] = (
                round(sum(scores) / len(scores), 2) if scores else 0.0
            )
    except Exception as exc:
        logger.warning("SKU %s 处理失败: %s", sku_id, exc)
        entry["error"] = str(exc)

    return entry


async def main_async(args: argparse.Namespace) -> None:
    root = get_root()
    ts = datetime.now().strftime("%Y%m%d%H")
    output_dir = root / "eval" / "results" / ts
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "eval_results.json"

    store = LocalDataStore()
    # LocalDataStore 已停用本地加载，SKU 数据统一走 ES；这里一次性扫描全量 SKU
    # 灌入 store.skus，供 sample_skus 分组采样与缺陷分析的 anchor_row 查询共用。
    es_for_scan = EsClient()
    if not es_for_scan.available:
        logger.error("ES 不可用，无法加载 SKU 数据，退出")
        return
    sku_rows = es_for_scan.scan_skus()
    if not sku_rows:
        logger.error("ES SKU 扫描结果为空，退出")
        return
    store.skus = {str(r.get("sku_id") or ""): r for r in sku_rows if r.get("sku_id")}
    logger.info("从 ES 加载 SKU: %d 个", len(store.skus))
    sampled = sample_skus(store)

    if args.limit and args.limit > 0:
        sampled = sampled[: args.limit]
        logger.info("--limit %d: 只评测前 %d 个 SKU", args.limit, len(sampled))

    if not sampled:
        logger.error("采样结果为空，退出")
        return

    defect_analyzer: OutfitDefectAnalyzer | None = None
    if args.defect_check:
        defect_analyzer = OutfitDefectAnalyzer(sku_store=store.skus)
        logger.info("属性缺陷检测已启用")

    aesthetic_analyzer: AestheticAnalyzer | None = None
    if args.aesthetic_check:
        aesthetic_analyzer = AestheticAnalyzer()
        logger.info("风格美学评估已启用")

    if args.skip_llm_rank_reason:
        logger.info("已跳过 LLM 排序和理由（使用规则排序）")

    workers = args.workers if args.workers and args.workers > 0 else 16
    logger.info("并行线程数: %d, SKU 总数: %d", workers, len(sampled))

    results: list[dict[str, Any]] = []
    es_success_count = 0
    es_error_count = 0
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_sku = {
            executor.submit(
                _process_single_sku,
                sku,
                store.skus,
                defect_analyzer,
                aesthetic_analyzer,
                skip_llm_rank_reason=args.skip_llm_rank_reason,
            ): sku
            for sku in sampled
        }
        with tqdm(total=len(sampled), desc="SKU 评测进度", unit="sku") as pbar:
            for future in as_completed(future_to_sku):
                entry = future.result()
                with lock:
                    results.append(entry)
                    es_success_count += entry["outfit_es_index"]["success"]
                    es_error_count += entry["outfit_es_index"]["error"]
                pbar.update(1)

    # ── 按中类拆分结果 ──
    by_cat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in results:
        sku = entry.get("input_sku") or {}
        up_down = sku.get("up_down") or ""
        cat_l2 = sku.get("category_l2") or ""
        key = f"{up_down}__{cat_l2}"
        by_cat[key].append(entry)

    cat_index: list[dict[str, Any]] = []
    for key in sorted(by_cat.keys()):
        items = by_cat[key]
        parts = key.split("__", 1)
        up_down, cat_l2 = parts[0], parts[1] if len(parts) > 1 else ""
        filename = _make_filename(up_down, cat_l2)
        file_path = output_dir / filename
        file_path.write_text(
            json.dumps(items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        cat_index.append({
            "up_down": up_down,
            "category_l2": cat_l2,
            "file": filename,
            "sku_count": len(items),
            "outfit_count": sum(len(r.get("outfit_ids") or []) for r in items),
            "error_count": sum(1 for r in items if r.get("error")),
        })

    index_data = {
        "eval_time": datetime.now(timezone.utc).isoformat(),
        "skip_llm_rank_reason": args.skip_llm_rank_reason,
        "total_skus": len(results),
        "success_count": sum(1 for r in results if not r["error"]),
        "error_count": sum(1 for r in results if r["error"]),
        "outfit_es_index": {
            "success": es_success_count,
            "error": es_error_count,
        },
        "categories": cat_index,
    }

    # 缺陷汇总（仅 --defect-check 模式）
    if args.defect_check:
        total_defect_outfits = sum(
            len(e.get("defects") or []) for e in results
        )
        total_defect_count = sum(e.get("defect_count", 0) for e in results)
        by_type: dict[str, int] = {dt: 0 for dt in (
            "gender_conflict", "season_mismatch", "role_missing",
            "category_l2_violation", "color_series_conflict", "price_overrun",
        )}
        for entry in results:
            for outfit_defect in entry.get("defects") or []:
                for d in outfit_defect.get("defects") or []:
                    dt = d.get("defect_type")
                    if dt in by_type:
                        by_type[dt] += 1
        index_data["defect_summary"] = {
            "outfits_with_defects": total_defect_outfits,
            "total_defect_count": total_defect_count,
            "by_type": by_type,
        }

    # 美学汇总（仅 --aesthetic-check 模式）
    if args.aesthetic_check:
        all_scores = [
            e.get("aesthetic_avg_score", 0.0)
            for e in results
            if e.get("aesthetic_avg_score", 0.0) > 0
        ]
        total_aesthetic_outfits = sum(
            len(e.get("aesthetics") or []) for e in results
        )
        dim_avgs: dict[str, list[float]] = {}
        for entry in results:
            for a in entry.get("aesthetics") or []:
                dims = a.get("dimensions") or {}
                for dim, val in dims.items():
                    if isinstance(val, dict) and val.get("score", 0) > 0:
                        dim_avgs.setdefault(dim, []).append(val["score"])
        by_dim = {
            dim: {
                "avg": round(sum(scores) / len(scores), 2),
                "count": len(scores),
            }
            for dim, scores in dim_avgs.items()
            if scores
        }
        index_data["aesthetic_summary"] = {
            "outfits_evaluated": total_aesthetic_outfits,
            "avg_overall_score": round(sum(all_scores) / len(all_scores), 2) if all_scores else 0.0,
            "by_dimension": by_dim,
        }

    outfit_rec_path: Path | None = None
    if args.save_json:
        outfit_rec_path = output_dir / f"outfit_rec_{ts}.json"
        export_data = _build_outfit_rec_export(
            results,
            eval_time=index_data["eval_time"],
            skip_llm_rank_reason=args.skip_llm_rank_reason,
        )
        outfit_rec_path.write_text(
            json.dumps(export_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        index_data["outfit_rec_file"] = outfit_rec_path.name

    output_path.write_text(
        json.dumps(index_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log_msg = "评测完成: %d/%d 成功, %d 个分类文件, 索引保存到 %s"
    log_args: list[Any] = [
        index_data["success_count"],
        index_data["total_skus"],
        len(cat_index),
        output_path,
    ]
    if outfit_rec_path is not None:
        log_msg += ", 搭配结果导出到 %s"
        log_args.append(outfit_rec_path)
    logger.info(log_msg, *log_args)


def main() -> None:
    parser = argparse.ArgumentParser(description="批量评测搭配推荐系统")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="只跑前 N 个 SKU（快速测试用，0 表示全部）",
    )
    parser.add_argument(
        "--defect-check",
        action="store_true",
        help="启用属性缺陷自动检测（gender/season/role/category_l2/color_series/price）",
    )
    parser.add_argument(
        "--aesthetic-check",
        action="store_true",
        help="启用风格美学 LLM 自动评分（style_consistency/color_harmony/occasion_fit/overall_aesthetics/creativity/proportion_balance）",
    )
    parser.add_argument(
        "--skip-llm-rank-reason",
        action="store_true",
        help="跳过 LLM 排序和理由，仅使用规则排序（默认不跳过）",
    )
    parser.add_argument(
        "--save-json",
        type=_parse_bool_arg,
        default=False,
        metavar="{true,false}",
        help="是否将输入 SKU 与搭配推荐结果写入 outfit_rec_{YYMMDDHH}.json（默认 false）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        help="并行线程数（默认 CPU 核数 × 2，设为 1 则单线程串行）",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="打印所有模块的详细日志（默认仅打印 batch_eval 的 INFO 及以上日志）",
    )
    args = parser.parse_args()
    if args.verbose:
        _configure_verbose_logging()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
