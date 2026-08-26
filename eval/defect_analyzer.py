"""搭配属性缺陷自动检测引擎。

对推荐产出的搭配进行 6 维属性匹配缺陷检查：
  1. gender_conflict      — 单品性别与意图性别冲突 (HIGH)
  2. season_mismatch      — 单品季节与意图季节无交集 (MEDIUM)
  3. role_missing         — 搭配缺少核心角色 top/bottoms/shoes (MEDIUM)
  4. category_l2_violation — 单品中类不在锚点互补中类白名单中 (HIGH)
  5. color_series_conflict — 单品色系不在锚点互补色系白名单中 (LOW)
  6. price_overrun         — 搭配总价超出意图预算 (LOW)

用法:
    from eval.defect_analyzer import OutfitDefectAnalyzer
    analyzer = OutfitDefectAnalyzer()
    defects = analyzer.analyze(outfit, intent, anchor_sku_row)
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from backend.models import UserIntent, normalize_gender, normalize_season
from backend.ranking.scoring import gender_conflict
from backend.intent.category_l2_pairing import get_companion_categories
from backend.intent.color_series_pairing import get_companion_color_series

logger = logging.getLogger(__name__)

# 核心搭配角色
_CORE_ROLES = frozenset({"top", "bottoms", "shoes"})

DEFECT_TYPES = (
    "gender_conflict",
    "season_mismatch",
    "role_missing",
    "category_l2_violation",
    "color_series_conflict",
    "price_overrun",
)


@dataclass
class Defect:
    """单条缺陷记录。"""

    defect_type: str
    severity: str
    item_sku_id: Optional[str]
    item_title: str
    detail: str
    suggestion: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DefectReport:
    """一套搭配的缺陷分析结果。"""

    outfit_id: str
    input_sku_id: str
    defects: list[Defect] = field(default_factory=list)

    @property
    def has_defects(self) -> bool:
        return len(self.defects) > 0

    @property
    def defect_count(self) -> int:
        return len(self.defects)

    @property
    def max_severity(self) -> str:
        order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
        if not self.defects:
            return "NONE"
        return max(self.defects, key=lambda d: order.get(d.severity, 0)).severity

    def to_dict(self) -> dict[str, Any]:
        return {
            "outfit_id": self.outfit_id,
            "input_sku_id": self.input_sku_id,
            "defect_count": self.defect_count,
            "max_severity": self.max_severity,
            "defects": [d.to_dict() for d in self.defects],
        }


class OutfitDefectAnalyzer:
    """搭配属性缺陷分析器。

    Args:
        sku_store: SKU 属性字典 {sku_id: row}，用于补全 outfit card 中缺失的属性字段。
                   若不传入，则仅基于 outfit item 自身字段检测。
    """

    def __init__(self, sku_store: dict[str, dict[str, Any]] | None = None) -> None:
        self._sku_store = sku_store or {}

    def _enrich_item(self, item: dict[str, Any]) -> dict[str, Any]:
        """用 sku_store 补全 item 中缺失的属性字段。"""
        sku_id = str(item.get("sku_id") or "").strip()
        if not sku_id or sku_id not in self._sku_store:
            return item
        store_row = self._sku_store[sku_id]
        enriched = dict(item)
        for key in ("gender", "season", "category_l2", "color_series", "color_name"):
            if not enriched.get(key) and store_row.get(key):
                enriched[key] = store_row[key]
        return enriched

    def analyze(
        self,
        outfit: dict[str, Any],
        intent: UserIntent | dict[str, Any] | None = None,
        anchor_sku_row: dict[str, Any] | None = None,
    ) -> DefectReport:
        """对一套搭配执行 6 维缺陷检测。

        Args:
            outfit: 搭配数据（raw outfit 或 outfit card）。
            intent: 用户意图（UserIntent 实例或 dict）。
            anchor_sku_row: 锚点 SKU 行数据。
        """
        oid = str(outfit.get("outfit_id") or "")
        input_sku_id = ""
        if anchor_sku_row:
            input_sku_id = str(anchor_sku_row.get("sku_id") or "")

        report = DefectReport(outfit_id=oid, input_sku_id=input_sku_id)

        # 归一化 intent
        intent_gender: str | None = None
        intent_season: list[str] = []
        intent_budget: float | None = None
        if isinstance(intent, UserIntent):
            intent_gender = intent.gender
            intent_season = list(intent.season or [])
            intent_budget = intent.budget_max
        elif isinstance(intent, dict):
            intent_gender = intent.get("gender")
            intent_season = list(intent.get("season") or [])
            budget = intent.get("budget_max")
            intent_budget = float(budget) if budget is not None else None

        items_raw = outfit.get("items") or []
        items = [self._enrich_item(it) for it in items_raw if isinstance(it, dict)]

        # --- 1. Gender conflict ---
        report.defects.extend(self._check_gender(items, intent_gender))

        # --- 2. Season mismatch ---
        report.defects.extend(self._check_season(items, intent_season))

        # --- 3. Role missing ---
        report.defects.extend(self._check_role_missing(items))

        # --- 4. Category L2 pairing violation ---
        report.defects.extend(self._check_category_l2(items, anchor_sku_row))

        # --- 5. Color series conflict ---
        report.defects.extend(self._check_color_series(items, anchor_sku_row))

        # --- 6. Price overrun ---
        report.defects.extend(self._check_price(outfit, intent_budget))

        return report

    # ------------------------------------------------------------------
    # 各维度检测实现
    # ------------------------------------------------------------------

    def _check_gender(
        self,
        items: list[dict[str, Any]],
        intent_gender: str | None,
    ) -> list[Defect]:
        """检测单品性别与意图性别冲突。"""
        if not intent_gender:
            return []
        defects: list[Defect] = []
        for it in items:
            item_gender = it.get("gender")
            if not item_gender:
                continue
            if gender_conflict(item_gender, intent_gender):
                norm_g = normalize_gender(item_gender) or str(item_gender)
                norm_w = normalize_gender(intent_gender) or str(intent_gender)
                defects.append(Defect(
                    defect_type="gender_conflict",
                    severity="HIGH",
                    item_sku_id=str(it.get("sku_id") or ""),
                    item_title=str(it.get("title") or ""),
                    detail=f"单品性别={norm_g}，意图性别={norm_w}，存在冲突",
                    suggestion=f"替换为性别匹配「{norm_w}」的同类单品",
                ))
        return defects

    def _check_season(
        self,
        items: list[dict[str, Any]],
        intent_season: list[str],
    ) -> list[Defect]:
        """检测单品季节与意图季节无交集。"""
        if not intent_season:
            return []
        defects: list[Defect] = []
        for it in items:
            raw_season = it.get("season")
            if not raw_season:
                continue
            item_seasons = normalize_season(raw_season)
            if not item_seasons:
                continue
            # 检查是否有交集
            has_overlap = False
            for want in intent_season:
                for elem in item_seasons:
                    if want == elem or want in elem or elem in want:
                        has_overlap = True
                        break
                if has_overlap:
                    break
            if not has_overlap:
                defects.append(Defect(
                    defect_type="season_mismatch",
                    severity="MEDIUM",
                    item_sku_id=str(it.get("sku_id") or ""),
                    item_title=str(it.get("title") or ""),
                    detail=(
                        f"单品季节={item_seasons}，"
                        f"意图季节={intent_season}，无交集"
                    ),
                    suggestion=f"替换为包含季节「{'/'.join(intent_season)}」的同类单品",
                ))
        return defects

    def _check_role_missing(
        self,
        items: list[dict[str, Any]],
    ) -> list[Defect]:
        """检测搭配是否缺少核心角色 (top/bottoms/shoes)。"""
        present_roles: set[str] = set()
        for it in items:
            role = str(it.get("role") or "").strip().lower()
            if role:
                present_roles.add(role)
        missing = _CORE_ROLES - present_roles
        if not missing:
            return []
        role_labels = {"top": "上装", "bottoms": "下装", "shoes": "鞋"}
        return [
            Defect(
                defect_type="role_missing",
                severity="MEDIUM",
                item_sku_id=None,
                item_title="",
                detail=f"搭配缺少核心角色：{role_labels.get(r, r)}",
                suggestion=f"补充一个「{role_labels.get(r, r)}」类单品以提升搭配完整度",
            )
            for r in sorted(missing)
        ]

    def _check_category_l2(
        self,
        items: list[dict[str, Any]],
        anchor_sku_row: dict[str, Any] | None,
    ) -> list[Defect]:
        """检测单品中类是否违反互补中类搭配规则。"""
        anchor_cat = ""
        if anchor_sku_row and not anchor_sku_row.get("_is_virtual_image_anchor"):
            anchor_cat = str(anchor_sku_row.get("category_l2") or "").strip()
        if not anchor_cat:
            # 尝试从 items 中找 is_anchor / is_master 的中类
            for it in items:
                if it.get("is_anchor") or it.get("is_master"):
                    anchor_cat = str(it.get("category_l2") or "").strip()
                    if anchor_cat:
                        break
        if not anchor_cat:
            return []
        companions = get_companion_categories(anchor_cat)
        if not companions:
            return []
        # 白名单：互补中类 + 锚点自身中类
        whitelist = set(companions) | {anchor_cat}
        defects: list[Defect] = []
        for it in items:
            cat = str(it.get("category_l2") or "").strip()
            if not cat:
                continue
            if cat not in whitelist:
                defects.append(Defect(
                    defect_type="category_l2_violation",
                    severity="HIGH",
                    item_sku_id=str(it.get("sku_id") or ""),
                    item_title=str(it.get("title") or ""),
                    detail=(
                        f"锚点中类={anchor_cat}，互补白名单={sorted(whitelist)}，"
                        f"单品中类={cat} 不在白名单中"
                    ),
                    suggestion=f"替换为中类属于互补白名单的同类单品（如 {companions[:3]}）",
                ))
        return defects

    def _check_color_series(
        self,
        items: list[dict[str, Any]],
        anchor_sku_row: dict[str, Any] | None,
    ) -> list[Defect]:
        """检测单品色系是否违反互补色系搭配规则。"""
        anchor_cs = ""
        if anchor_sku_row and not anchor_sku_row.get("_is_virtual_image_anchor"):
            anchor_cs = str(anchor_sku_row.get("color_series") or "").strip()
        if not anchor_cs:
            for it in items:
                if it.get("is_anchor") or it.get("is_master"):
                    anchor_cs = str(it.get("color_series") or "").strip()
                    if anchor_cs:
                        break
        if not anchor_cs:
            return []
        companions = get_companion_color_series(anchor_cs)
        if not companions:
            return []
        whitelist = set(companions) | {anchor_cs}
        defects: list[Defect] = []
        for it in items:
            cs = str(it.get("color_series") or "").strip()
            if not cs:
                continue
            if cs not in whitelist:
                defects.append(Defect(
                    defect_type="color_series_conflict",
                    severity="LOW",
                    item_sku_id=str(it.get("sku_id") or ""),
                    item_title=str(it.get("title") or ""),
                    detail=(
                        f"锚点色系={anchor_cs}，互补白名单={sorted(whitelist)}，"
                        f"单品色系={cs} 不在白名单中"
                    ),
                    suggestion=f"替换为色系属于互补白名单的同类单品（如 {companions[:3]}）",
                ))
        return defects

    def _check_price(
        self,
        outfit: dict[str, Any],
        budget_max: float | None,
    ) -> list[Defect]:
        """检测搭配总价是否超出预算。"""
        if budget_max is None or budget_max <= 0:
            return []
        price_total = float(outfit.get("price_total") or 0.0)
        if price_total <= 0:
            return []
        if price_total > budget_max:
            overrun = price_total - budget_max
            pct = overrun / budget_max * 100
            return [Defect(
                defect_type="price_overrun",
                severity="LOW",
                item_sku_id=None,
                item_title="",
                detail=(
                    f"搭配总价=¥{price_total:.0f}，"
                    f"预算上限=¥{budget_max:.0f}，"
                    f"超出 ¥{overrun:.0f} ({pct:.0f}%)"
                ),
                suggestion="替换部分高价单品以降低总价，或调整预算上限",
            )]
        return []


def summarize_defects(reports: list[DefectReport]) -> dict[str, Any]:
    """汇总多套搭配的缺陷统计。"""
    total = len(reports)
    with_defects = sum(1 for r in reports if r.has_defects)
    by_type: dict[str, int] = {dt: 0 for dt in DEFECT_TYPES}
    by_severity: dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for r in reports:
        for d in r.defects:
            if d.defect_type in by_type:
                by_type[d.defect_type] += 1
            if d.severity in by_severity:
                by_severity[d.severity] += 1
    return {
        "total_outfits": total,
        "outfits_with_defects": with_defects,
        "defect_rate": round(with_defects / total, 4) if total > 0 else 0.0,
        "by_type": by_type,
        "by_severity": by_severity,
        "total_defect_count": sum(by_type.values()),
    }
