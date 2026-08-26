"""完整度、匹配分与排序分量。"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

import requests

from backend.models import normalize_age, normalize_genders, normalize_season

logger = logging.getLogger(__name__)


_COMPLETENESS_TABLE: list[tuple[frozenset[str], float]] = [
    (frozenset({"top", "bottoms", "shoes"}), 1.0),
    (frozenset({"dress", "shoes"}), 0.9),
    (frozenset({"top", "bottoms", "accessory"}), 0.75),
    (frozenset({"top", "bottoms"}), 0.7),
    (frozenset({"dress", "accessory"}), 0.65),
    (frozenset({"top", "shoes"}), 0.6),
    (frozenset({"bottoms", "shoes"}), 0.55),
]

_CORE_ROLES = frozenset({"top", "bottoms", "dress", "shoes"})


def outfit_completeness_score(roles: Set[str]) -> float:
    core = set(roles)
    for required, score in _COMPLETENESS_TABLE:
        if required <= core:
            return score
    if core & _CORE_ROLES:
        return 0.3
    return 0.1


def tryon_coverage_from_items(items: List[Dict[str, Any]]) -> float:
    if not items:
        return 0.0
    n = 0
    ok = 0
    for it in items:
        n += 1
        t = it.get("tryon_image")
        iq = (it.get("image_quality") or {}).get("is_tryon_ready")
        if t and iq is not False:
            ok += 1
    return ok / n if n else 0.0


def intent_attr_match(
    row: dict[str, Any],
    gender: Optional[str],
    season: List[str],
    tags: List[str],
    age: Optional[str] = None,
) -> float:
    score = 0.0
    if gender and row.get("gender"):
        g_set = normalize_genders(row["gender"])
        w_set = normalize_genders(gender)
        if not g_set:
            g_set = {str(row["gender"])}
        if not w_set:
            w_set = {str(gender)}
        if g_set & w_set:
            score += 0.25
    if age and row.get("age"):
        row_age = normalize_age(row["age"])
        want_age = normalize_age(age)
        if row_age and want_age:
            if want_age == "通码":
                hit = row_age == "通码"
            else:
                hit = row_age in (want_age, "通码")
            if hit:
                score += 0.10
    if season:
        rs = normalize_season(row.get("season"))
        hit = 0
        for want in season:
            if not want:
                continue
            for elem in rs:
                es = str(elem)
                if want == es or want in es or es in want:
                    hit += 1
                    break
        score += 0.10 * min(hit, 3)
    if tags:
        st = str(row.get("search_text") or "")
        hit = sum(1 for t in tags if t and t in st)
        score += 0.08 * min(hit, 4)
    return min(score, 1.0)


def price_match(price: Optional[float], budget: Optional[float]) -> float:
    if budget is None or budget <= 0 or price is None:
        return 0.5
    if price <= budget:
        return 1.0
    over = (price - budget) / budget
    return max(0.0, 1.0 - min(over, 1.0))


def gender_conflict(row_gender: Optional[str], want: Optional[str]) -> bool:
    if not row_gender or not want:
        return False
    g_set = normalize_genders(row_gender)
    w_set = normalize_genders(want)
    if not g_set:
        g_set = {str(row_gender)}
    if not w_set:
        w_set = {str(want)}
    if "儿童" in w_set:
        return False
    want_male = w_set & {"男", "男童"}
    want_female = w_set & {"女", "女童"}
    row_male = g_set & {"男", "男童"}
    row_female = g_set & {"女", "女童"}
    if want_male and not row_male and row_female:
        return True
    if want_female and not row_female and row_male:
        return True
    return False


def season_conflict(row_season: Optional[str], want_seasons: Optional[List[str]]) -> bool:
    if not row_season or not want_seasons:
        return False
    rs = normalize_season(row_season)
    if not rs:
        return False
    # 跨季兼容（春夏/秋冬配对）：春锚点与夏款不冲突，冬装外套仍挡夏款。
    # want 侧展开兼容集即可（交集对称）；row 侧已由 normalize_season 拆为单季 token。
    from backend.models import season_compatible_set

    want_set = set(season_compatible_set(list(want_seasons)))
    return not bool(set(rs) & want_set)


def age_conflict(row_age: Optional[str], want: Optional[str]) -> bool:
    """童装年龄段冲突判断（与 gender_conflict 对称）。

    want 为具体段（小童/中大童/婴幼童）时：行 age 必须等于 want 或通码，
    否则冲突（含空值——成人款不应进入童装段查询结果）。
    want 为通码时：行 age 必须为通码。
    任一空（未指定 age）时不冲突，交由其它维度过滤。
    """
    if not want:
        return False
    want_s = normalize_age(want)
    if not want_s:
        return False
    row_s = normalize_age(row_age)
    if not row_s:
        # 查询指定了童装段但行无年龄段（成人款/缺失）→ 视为冲突
        return True
    if want_s == "通码":
        return row_s != "通码"
    return row_s not in (want_s, "通码")


_NEUTRAL_SERIES = frozenset({"黑色系", "白色系", "灰色系", "米色系"})

_COMPLEMENTARY_PAIRS: set[frozenset[str]] = {
    frozenset({"红色系", "绿色系"}),
    frozenset({"蓝色系", "橙色系"}),
    frozenset({"黄色系", "紫色系"}),
}


def category_l2_match_score(items: List[Dict[str, Any]]) -> float:
    """评估搭配内非锚点单品的中类(category_l2)与锚点中类的互补匹配度。

    返回 0.0 ~ 1.0：
    - 所有非锚点单品中类均命中 primary 互补列表 → 1.0
    - 命中 allowed（非 primary）→ 0.6 折扣分
    - 锚点无互补规则 → 0.2（未知组合，低分）
    - 非锚点中类为空 → 计入分母但不计入分子（拉低分数）
    - 无锚点或无有效中类 → 0.2
    """
    from backend.intent.category_l2_pairing import get_companion_categories

    if not items:
        return 0.2

    anchor_cat2 = ""
    non_anchor_items: List[Dict[str, Any]] = []
    for it in items:
        if it.get("is_master") or it.get("is_anchor"):
            anchor_cat2 = str(it.get("category_l2") or "").strip()
        else:
            non_anchor_items.append(it)

    if not anchor_cat2:
        for it in items:
            cat = str(it.get("category_l2") or "").strip()
            if cat:
                anchor_cat2 = cat
                non_anchor_items = [
                    x for x in items if x is not it
                ]
                break

    if not anchor_cat2 or not non_anchor_items:
        return 0.2

    primary = get_companion_categories(anchor_cat2, list_mode="primary", adaptive=False)
    allowed = get_companion_categories(anchor_cat2, list_mode="allowed", adaptive=False)
    if not primary and not allowed:
        return 0.2

    primary_set = set(primary or [])
    allowed_set = set(allowed or []) - primary_set

    total = 0
    score_sum = 0.0
    for it in non_anchor_items:
        cat = str(it.get("category_l2") or "").strip()
        total += 1
        if cat and cat in primary_set:
            score_sum += 1.0
        elif cat and cat in allowed_set:
            score_sum += 0.6

    if total == 0:
        return 0.2
    return score_sum / total


def color_harmony_score(items: List[Dict[str, Any]]) -> float:
    """评估搭配内单品的色系和谐度。

    返回 0.0 ~ 1.0：
    - 2-3 种色系 → 高分（配色丰富且不混乱）
    - 1 种色系   → 中分（同色系，安全但单调）
    - >= 4 种    → 低分（过于复杂）
    - 中性色+彩色 → 加分
    - 互补色组合  → 加分
    """
    if not items:
        return 0.5

    series_set: Set[str] = set()
    for it in items:
        cs = it.get("color_series") or []
        if isinstance(cs, str):
            cs = [cs] if cs else []
        for c in cs:
            c = str(c).strip()
            if c:
                series_set.add(c)

    if not series_set:
        return 0.5

    n = len(series_set)
    if n == 1:
        base = 0.55
    elif n == 2:
        base = 0.85
    elif n == 3:
        base = 0.75
    else:
        base = max(0.3, 0.75 - 0.15 * (n - 3))

    bonus = 0.0

    neutrals = series_set & _NEUTRAL_SERIES
    chromatics = series_set - _NEUTRAL_SERIES
    if neutrals and chromatics:
        bonus += 0.1

    for pair in _COMPLEMENTARY_PAIRS:
        if pair <= series_set:
            bonus += 0.05
            break

    return min(base + bonus, 1.0)


# ---------------------------------------------------------------------------
# Model-based compatibility scoring (calls external FastAPI service)
# ---------------------------------------------------------------------------


def model_compatibility_scores(
    outfits: List[Dict[str, Any]],
    service_url: str,
    timeout: float = 5.0,
) -> Dict[str, float]:
    """Call the outfit-transformer scoring service and return {outfit_id: score}.

    Each outfit's items are sent with ``tryon_image`` as ``image_url`` and
    ``title`` (Chinese) as ``description`` — the service translates internally.

    Returns an empty dict on failure so the caller can fall back to rule-based
    scoring.
    """
    payload_outfits = []
    for o in outfits:
        oid = str(o.get("outfit_id") or "")
        items_payload = []
        for it in o.get("items") or []:
            img = it.get("tryon_image") or ""
            desc = it.get("title") or ""
            # Outfit-template items carry their goods code in ``attrAlias``/
            # ``idAlias`` with ``sku_id`` left None; the embedding cache is
            # keyed by this full code. Fall back through the same chain as
            # ``DataFacade._item_sku_id`` so catalog/cache items hit the
            # precomputed embedding and skip translate+download+encode.
            sku_id = str(
                it.get("sku_id") or it.get("skuId")
                or it.get("attrAlias") or it.get("idAlias") or ""
            ).strip()
            if img and (img.startswith("http://") or img.startswith("https://") or img.startswith("data:")):
                item_payload = {"image_url": img, "description": desc}
                if sku_id:
                    item_payload["sku_id"] = sku_id
                items_payload.append(item_payload)
        if items_payload:
            payload_outfits.append({"outfit_id": oid, "items": items_payload})

    if not payload_outfits:
        return {}

    url = f"{service_url.rstrip('/')}/score"
    try:
        resp = requests.post(
            url,
            json={"outfits": payload_outfits},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return {s["outfit_id"]: float(s["score"]) for s in data.get("scores", [])}
    except Exception:
        logger.warning("Model scoring service call failed", exc_info=True)
        return {}
