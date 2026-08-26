"""批量风格美学评估：从已有评测结果或实时 pipeline 执行 LLM 美学评分。

用法:
    cd fila_agent_html

    # 模式 1：从已有评测结果文件分析
    python -m eval.batch_aesthetic_eval --input eval/results/eval_results.json

    # 模式 2：从分类文件目录分析
    python -m eval.batch_aesthetic_eval --input eval/results/

    # 模式 3：端到端 pipeline（采样 SKU → 跑推荐 → 美学评分）
    python -m eval.batch_aesthetic_eval --from-pipeline --n-per-group 2 --limit 5

    # 指定输出路径
    python -m eval.batch_aesthetic_eval --input eval/results/ --output eval/results/aesthetic_report.json
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
from eval.aesthetic_analyzer import (
    AESTHETIC_DIMS,
    AestheticAnalyzer,
    AestheticReport,
    summarize_aesthetic,
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("batch_aesthetic_eval")
logger.setLevel(logging.INFO)


def _load_eval_results(input_path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if input_path.is_dir():
        for fp in sorted(input_path.glob("*.json")):
            entries.extend(_load_single_file(fp))
    else:
        entries.extend(_load_single_file(input_path))
    return entries


def _load_single_file(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        logger.warning("文件不存在: %s", path)
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
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
        return [data]
    return []


def _extract_outfits_from_entry(entry: dict[str, Any]) -> list[dict[str, Any]]:
    outfits = entry.get("outfits")
    if isinstance(outfits, list) and outfits:
        return [x for x in outfits if isinstance(x, dict)]
    snapshots: list[dict[str, Any]] = []
    for meta in entry.get("outfit_meta") or []:
        if isinstance(meta, dict) and isinstance(meta.get("snapshot"), dict):
            snapshots.append(meta["snapshot"])
    return snapshots


def _extract_intent_from_entry(entry: dict[str, Any]) -> dict[str, Any]:
    intent = entry.get("intent") or {}
    if isinstance(intent, dict):
        return intent
    return {}


def analyze_from_results(
    input_path: Path,
) -> list[AestheticReport]:
    entries = _load_eval_results(input_path)
    if not entries:
        logger.warning("未加载到任何评测结果: %s", input_path)
        return []

    analyzer = AestheticAnalyzer()
    reports: list[AestheticReport] = []

    for entry in entries:
        outfits = _extract_outfits_from_entry(entry)
        intent_dict = _extract_intent_from_entry(entry)

        for outfit in outfits:
            report = analyzer.analyze(outfit, intent_dict)
            reports.append(report)

    logger.info(
        "美学评估完成: %d 套搭配, 有效 %d, 错误 %d",
        len(reports),
        sum(1 for r in reports if r.is_valid),
        sum(1 for r in reports if r.error),
    )
    return reports


async def analyze_from_pipeline(
    n_per_group: int,
    limit: int,
    seed: int,
) -> list[AestheticReport]:
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
    analyzer = AestheticAnalyzer()
    reports: list[AestheticReport] = []

    from tqdm import tqdm
    for sku in tqdm(sampled, desc="美学评估进度", unit="sku"):
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

        for outfit in result.get("outfits") or []:
            if not isinstance(outfit, dict):
                continue
            report = analyzer.analyze(outfit, intent_dict)
            reports.append(report)

    logger.info("Pipeline 美学评估完成: %d 套搭配", len(reports))
    return reports


def build_report_json(
    reports: list[AestheticReport],
) -> dict[str, Any]:
    summary = summarize_aesthetic(reports)
    details = [r.to_dict() for r in reports]
    return {
        "eval_time": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "details": details,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="搭配风格美学批量评估（LLM 自动评分）",
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
        help="端到端模式：采样 SKU → 跑推荐 → 美学评分",
    )
    parser.add_argument(
        "--output",
        default="eval/results/aesthetic_report.json",
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
    args = parser.parse_args()

    root = get_root()
    output_path = root / args.output

    if args.from_pipeline:
        reports = asyncio.run(analyze_from_pipeline(
            n_per_group=args.n_per_group,
            limit=args.limit,
            seed=args.seed,
        ))
    else:
        input_path = Path(args.input)
        if not input_path.is_absolute():
            input_path = root / input_path
        reports = analyze_from_results(input_path)

    report_data = build_report_json(reports)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    s = report_data["summary"]
    logger.info(
        "美学评估报告已保存: %s\n"
        "  搭配总数: %d\n"
        "  有效评估: %d\n"
        "  错误数: %d\n"
        "  平均总分: %.2f\n"
        "  各维度均分: %s",
        output_path,
        s["total_outfits"],
        s["valid_count"],
        s["error_count"],
        s["avg_overall_score"],
        json.dumps(s.get("by_dimension", {}), ensure_ascii=False),
    )


if __name__ == "__main__":
    main()
