"""批量缺陷评估：从已有评测结果或实时 pipeline 执行属性缺陷检测。

用法:
    cd fila_agent_html

    # 模式 1：从已有评测结果文件分析
    python -m eval.batch_defect_eval --input eval/results/eval_results.json

    # 模式 2：从分类文件目录分析（自动加载目录下所有 .json 文件）
    python -m eval.batch_defect_eval --input eval/results/

    # 模式 3：端到端 pipeline（采样 SKU → 跑推荐 → 缺陷检测）
    python -m eval.batch_defect_eval --from-pipeline --n-per-group 2 --limit 5

    # 指定输出路径
    python -m eval.batch_defect_eval --input eval/results/ --output eval/results/defect_report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import get_root
from backend.local_data_store import LocalDataStore
from backend.models import UserIntent
from eval.defect_analyzer import (
    DEFECT_TYPES,
    DefectReport,
    OutfitDefectAnalyzer,
    summarize_defects,
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("batch_defect_eval")
logger.setLevel(logging.INFO)


def _load_eval_results(input_path: Path) -> list[dict[str, Any]]:
    """加载评测结果文件（支持单文件或目录下所有 .json）。"""
    entries: list[dict[str, Any]] = []
    if input_path.is_dir():
        for fp in sorted(input_path.glob("*.json")):
            entries.extend(_load_single_file(fp))
    else:
        entries.extend(_load_single_file(input_path))
    return entries


def _load_single_file(path: Path) -> list[dict[str, Any]]:
    """加载单个评测结果 JSON 文件。"""
    if not path.is_file():
        logger.warning("文件不存在: %s", path)
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        # 索引文件（eval_results.json）包含 categories 字段，需加载各分类文件
        cats = data.get("categories") or []
        if cats:
            parent = path.parent
            entries: list[dict[str, Any]] = []
            for cat in cats:
                fn = cat.get("file")
                if fn:
                    fp = parent / fn
                    entries.extend(_load_single_file(fp))
            return entries
        # 单条结果
        return [data]
    return []


def _extract_outfits_from_entry(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """从评测结果条目中提取搭配数据。"""
    # 优先使用 outfits 字段（完整 outfit 数据）
    outfits = entry.get("outfits")
    if isinstance(outfits, list) and outfits:
        return [x for x in outfits if isinstance(x, dict)]
    # 回退到 outfit_meta 中的 snapshot
    snapshots: list[dict[str, Any]] = []
    for meta in entry.get("outfit_meta") or []:
        if isinstance(meta, dict) and isinstance(meta.get("snapshot"), dict):
            snapshots.append(meta["snapshot"])
    return snapshots


def _extract_intent_from_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """从评测结果条目中提取意图数据。"""
    intent = entry.get("intent") or {}
    if isinstance(intent, dict):
        return intent
    return {}


def _extract_anchor_from_entry(
    entry: dict[str, Any],
    sku_store: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """从评测结果条目中提取锚点 SKU 行数据。"""
    input_sku_id = str(entry.get("input_sku_id") or "").strip()
    if input_sku_id and input_sku_id in sku_store:
        return sku_store[input_sku_id]
    # 回退到 input_sku 字段
    input_sku = entry.get("input_sku")
    if isinstance(input_sku, dict) and input_sku:
        return input_sku
    return None


def analyze_from_results(
    input_path: Path,
    sku_store: dict[str, dict[str, Any]],
) -> list[DefectReport]:
    """从已有评测结果文件执行缺陷分析。"""
    entries = _load_eval_results(input_path)
    if not entries:
        logger.warning("未加载到任何评测结果: %s", input_path)
        return []

    analyzer = OutfitDefectAnalyzer(sku_store=sku_store)
    reports: list[DefectReport] = []

    for entry in entries:
        outfits = _extract_outfits_from_entry(entry)
        intent_dict = _extract_intent_from_entry(entry)
        anchor_row = _extract_anchor_from_entry(entry, sku_store)

        for outfit in outfits:
            report = analyzer.analyze(outfit, intent_dict, anchor_row)
            reports.append(report)

    logger.info("分析完成: %d 套搭配, %d 条缺陷", len(reports),
                sum(r.defect_count for r in reports))
    return reports


async def analyze_from_pipeline(
    n_per_group: int,
    limit: int,
    seed: int,
    sku_store: dict[str, dict[str, Any]],
) -> list[DefectReport]:
    """端到端：采样 SKU → 跑推荐 pipeline → 缺陷检测。"""
    from eval.batch_eval import sample_skus, run_pipeline_for_sku, download_image_base64
    from backend.services.recommend_service import RecommendService

    store = LocalDataStore()
    sampled = sample_skus(store, n_per_group=n_per_group, seed=seed)
    if limit and limit > 0:
        sampled = sampled[:limit]
    if not sampled:
        logger.error("采样结果为空")
        return []

    svc = RecommendService()
    analyzer = OutfitDefectAnalyzer(sku_store=sku_store)
    reports: list[DefectReport] = []

    from tqdm import tqdm
    for sku in tqdm(sampled, desc="缺陷评估进度", unit="sku"):
        sku_id = sku["sku_id"]
        tryon_url = (sku.get("tryon_image") or "").strip()
        if not tryon_url:
            continue
        try:
            image_b64 = download_image_base64(tryon_url)
            result = await run_pipeline_for_sku(svc, sku, image_b64)
        except Exception as exc:
            logger.warning("SKU %s pipeline 失败: %s", sku_id, exc)
            continue

        intent_dict = result.get("intent") or {}
        anchor_row = sku_store.get(str(sku_id))

        for outfit in result.get("outfits") or []:
            if not isinstance(outfit, dict):
                continue
            report = analyzer.analyze(outfit, intent_dict, anchor_row)
            reports.append(report)

    logger.info("Pipeline 分析完成: %d 套搭配", len(reports))
    return reports


def build_report_json(
    reports: list[DefectReport],
    *,
    include_clean: bool = False,
) -> dict[str, Any]:
    """构建完整的缺陷报告 JSON。"""
    summary = summarize_defects(reports)
    details = [r.to_dict() for r in reports if r.has_defects or include_clean]
    return {
        "eval_time": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "details": details,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="搭配属性缺陷批量评估",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--input",
        help="评测结果 JSON 文件或目录（相对 fila_agent_html/）",
    )
    group.add_argument(
        "--from-pipeline",
        action="store_true",
        help="端到端模式：采样 SKU → 跑推荐 → 缺陷检测",
    )
    parser.add_argument(
        "--output",
        default="eval/results/defect_report.json",
        help="输出 JSON 路径 (相对 fila_agent_html/)",
    )
    parser.add_argument(
        "--n-per-group",
        type=int,
        default=2,
        help="pipeline 模式：每组采样数 (默认 2)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="pipeline 模式：最多跑 N 个 SKU (0=全部)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="pipeline 模式：随机种子",
    )
    parser.add_argument(
        "--include-clean",
        action="store_true",
        help="在报告中包含无缺陷的搭配（默认只输出有缺陷的）",
    )
    args = parser.parse_args()

    root = get_root()
    output_path = root / args.output

    # 加载 SKU 属性索引
    store = LocalDataStore()
    store.load()
    sku_store = store.skus
    logger.info("SKU 索引加载完成: %d 条", len(sku_store))

    if args.from_pipeline:
        reports = asyncio.run(analyze_from_pipeline(
            n_per_group=args.n_per_group,
            limit=args.limit,
            seed=args.seed,
            sku_store=sku_store,
        ))
    else:
        input_path = Path(args.input)
        if not input_path.is_absolute():
            input_path = root / input_path
        reports = analyze_from_results(input_path, sku_store)

    report_data = build_report_json(
        reports,
        include_clean=args.include_clean,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 输出摘要
    s = report_data["summary"]
    logger.info(
        "缺陷报告已保存: %s\n"
        "  搭配总数: %d\n"
        "  有缺陷: %d (%.1f%%)\n"
        "  缺陷总数: %d\n"
        "  按类型: %s",
        output_path,
        s["total_outfits"],
        s["outfits_with_defects"],
        s["defect_rate"] * 100,
        s["total_defect_count"],
        json.dumps(s["by_type"], ensure_ascii=False),
    )


if __name__ == "__main__":
    main()
