"""搭配风格美学 LLM 自动评估引擎。

对推荐产出的搭配进行 6 维风格美学评分：
  1. style_consistency   — 风格统一性
  2. color_harmony       — 色彩和谐度
  3. occasion_fit        — 场合适配性
  4. overall_aesthetics  — 整体美观度
  5. creativity          — 搭配创意性
  6. proportion_balance  — 比例协调性

用法:
    from eval.aesthetic_analyzer import AestheticAnalyzer
    analyzer = AestheticAnalyzer()
    result = analyzer.analyze(outfit, intent)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from backend.llm_client import _chat_block
from backend.prompt_loader import load_named_prompt

logger = logging.getLogger(__name__)

AESTHETIC_DIMS = (
    "style_consistency",
    "color_harmony",
    "occasion_fit",
    "overall_aesthetics",
    "creativity",
    "proportion_balance",
)


@dataclass
class DimScore:
    score: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AestheticReport:
    outfit_id: str
    input_sku_id: str
    style_consistency: DimScore | None = None
    color_harmony: DimScore | None = None
    occasion_fit: DimScore | None = None
    overall_aesthetics: DimScore | None = None
    creativity: DimScore | None = None
    proportion_balance: DimScore | None = None
    overall_score: float = 0.0
    summary: str = ""
    error: str = ""

    @property
    def is_valid(self) -> bool:
        return self.overall_score > 0 and not self.error

    def to_dict(self) -> dict[str, Any]:
        dims = {}
        for dim in AESTHETIC_DIMS:
            val = getattr(self, dim, None)
            dims[dim] = val.to_dict() if val else None
        return {
            "outfit_id": self.outfit_id,
            "input_sku_id": self.input_sku_id,
            "dimensions": dims,
            "overall_score": self.overall_score,
            "summary": self.summary,
            "error": self.error,
        }


class AestheticAnalyzer:
    """搭配风格美学 LLM 评估器。"""

    def __init__(self, model_override: str | None = None) -> None:
        self._model_override = model_override

    def _format_outfit_for_prompt(
        self,
        outfit: dict[str, Any],
        intent: dict[str, Any] | None = None,
    ) -> str:
        lines = [
            f"搭配名称: {outfit.get('name') or outfit.get('outfit_id') or ''}",
            f"搭配总价: ¥{outfit.get('price_total') or '未知'}",
            f"召回来源: {outfit.get('recall_source') or outfit.get('source') or ''}",
        ]
        if intent:
            gender = intent.get("gender")
            season = intent.get("season")
            occasion = intent.get("occasion_tags")
            style = intent.get("style_tags")
            if gender:
                lines.append(f"目标性别: {gender}")
            if season:
                lines.append(f"目标季节: {', '.join(season) if isinstance(season, list) else season}")
            if occasion:
                lines.append(f"目标场合: {', '.join(occasion) if isinstance(occasion, list) else occasion}")
            if style:
                lines.append(f"目标风格: {', '.join(style) if isinstance(style, list) else style}")
        lines.append("")
        lines.append("单品明细:")
        for it in outfit.get("items") or []:
            role = it.get("role") or ""
            title = it.get("title") or ""
            color = it.get("color_name") or it.get("color") or ""
            cat = it.get("category_l2") or it.get("category") or ""
            price = it.get("price") or ""
            parts = [f"[{role}]", title]
            if color:
                parts.append(f"颜色:{color}")
            if cat:
                parts.append(f"品类:{cat}")
            if price:
                parts.append(f"¥{price}")
            lines.append("  " + " | ".join(parts))
        return "\n".join(lines)

    def _parse_response(self, raw: str) -> dict[str, Any]:
        m = re.search(r"\{[\s\S]*\}", raw or "")
        if not m:
            return {}
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}

    def analyze(
        self,
        outfit: dict[str, Any],
        intent: dict[str, Any] | None = None,
    ) -> AestheticReport:
        oid = str(outfit.get("outfit_id") or "")
        input_sku_id = ""
        items = outfit.get("items") or []
        for it in items:
            if it.get("is_anchor") or it.get("is_master"):
                input_sku_id = str(it.get("sku_id") or "")
                break

        report = AestheticReport(outfit_id=oid, input_sku_id=input_sku_id)

        system = load_named_prompt("eval_aesthetic")
        user_text = self._format_outfit_for_prompt(outfit, intent)

        try:
            raw = _chat_block(
                "eval_llm",
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_text},
                ],
                temperature=0.3,
                model_override=self._model_override,
            )
        except Exception as exc:
            logger.warning("美学评估 LLM 调用失败 outfit=%s: %s", oid, exc)
            report.error = str(exc)
            return report

        if not raw:
            report.error = "LLM 返回空内容"
            return report

        parsed = self._parse_response(raw)
        if not parsed:
            report.error = f"LLM 返回无法解析: {raw[:200]}"
            return report

        for dim in AESTHETIC_DIMS:
            dim_data = parsed.get(dim)
            if isinstance(dim_data, dict):
                score = dim_data.get("score")
                reason = dim_data.get("reason") or ""
                try:
                    score_int = int(score)
                    score_int = max(1, min(5, score_int))
                except (TypeError, ValueError):
                    score_int = 0
                setattr(report, dim, DimScore(score=score_int, reason=str(reason)))

        overall = parsed.get("overall_score")
        if isinstance(overall, (int, float)):
            report.overall_score = round(float(overall), 1)
        else:
            scores = [
                getattr(report, dim).score
                for dim in AESTHETIC_DIMS
                if getattr(report, dim) and getattr(report, dim).score > 0
            ]
            if scores:
                report.overall_score = round(sum(scores) / len(scores), 1)

        report.summary = str(parsed.get("summary") or "")

        return report


def summarize_aesthetic(reports: list[AestheticReport]) -> dict[str, Any]:
    total = len(reports)
    valid = [r for r in reports if r.is_valid]
    valid_count = len(valid)
    if not valid:
        return {
            "total_outfits": total,
            "valid_count": 0,
            "error_count": total,
            "avg_overall_score": 0.0,
            "by_dimension": {},
        }

    avg_overall = round(sum(r.overall_score for r in valid) / valid_count, 2)

    dim_scores: dict[str, list[int]] = {dim: [] for dim in AESTHETIC_DIMS}
    for r in valid:
        for dim in AESTHETIC_DIMS:
            ds = getattr(r, dim)
            if ds and ds.score > 0:
                dim_scores[dim].append(ds.score)

    by_dim = {}
    for dim, scores in dim_scores.items():
        if scores:
            by_dim[dim] = {
                "avg": round(sum(scores) / len(scores), 2),
                "min": min(scores),
                "max": max(scores),
                "count": len(scores),
            }

    return {
        "total_outfits": total,
        "valid_count": valid_count,
        "error_count": total - valid_count,
        "avg_overall_score": avg_overall,
        "by_dimension": by_dim,
    }
