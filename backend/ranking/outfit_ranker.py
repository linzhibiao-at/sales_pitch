"""搭配排序。"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Set, Tuple

from backend.models import normalize_gender_first, normalize_season
from backend.ranking.scoring import category_l2_match_score, color_harmony_score, intent_attr_match, model_compatibility_scores

logger = logging.getLogger(__name__)


def _outfit_item_sku_set_key(outfit: Dict[str, Any]) -> Any:
    """用于去重：同一套单品 sku_id 集合视为同一路重复。"""
    ids = [
        str(it.get("sku_id") or "").strip()
        for it in (outfit.get("items") or [])
    ]
    ids = [x for x in ids if x]
    if not ids:
        oid = str(outfit.get("outfit_id") or "")
        return ("__no_items__", oid)
    return frozenset(ids)


def anchor_sku_is_master_in_outfit(
    outfit: Dict[str, Any],
    anchor_sku_id: str,
) -> bool:
    """当前搭配里，锚点 sku 是否以主商品（is_master）出现。"""
    aid = str(anchor_sku_id or "").strip()
    if not aid:
        return False
    for it in outfit.get("items") or []:
        if str(it.get("sku_id") or "").strip() != aid:
            continue
        return bool(it.get("is_master"))
    return False


def _pick_one_outfit_stable(
    candidates: List[Dict[str, Any]],
    source_match_ids: Set[str],
) -> Dict[str, Any]:
    """同组内稳定择优：优先图召回 id，其次非合成套装，再 outfit_id 字典序。"""
    def sort_key(o: Dict[str, Any]) -> tuple:
        oid = str(o.get("outfit_id") or "")
        in_src = 0 if oid in source_match_ids else 1
        is_synth = 0 if oid.startswith("synth_") else 1
        return (in_src, is_synth, oid)

    return min(candidates, key=sort_key)


def dedupe_outfits_same_skus_prefer_anchor_master(
    outfits: List[Dict[str, Any]],
    anchor_sku_id: str,
    *,
    source_match_ids: Optional[Set[str]] = None,
    vec_map: Optional[Dict[str, float]] = None,
) -> List[Dict[str, Any]]:
    """多路召回合并后、排序前：sku 集合相同的只保留一条。

    若存在重复，优先保留锚点 SKU 在 items 中 ``is_master=True`` 的搭配；
    若均不满足（或仅一条），按 ``_pick_one_outfit_stable`` 规则保留一条。
    若提供 ``vec_map``（RRF 分数），则用 RRF 分数择优替代硬编码规则。
    """
    if len(outfits) <= 1:
        return list(outfits)
    src = source_match_ids or set()
    anchor = str(anchor_sku_id or "").strip()
    rrf = vec_map or {}
    buckets: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    order: List[Any] = []
    for o in outfits:
        key = _outfit_item_sku_set_key(o)
        if key not in buckets:
            order.append(key)
        buckets[key].append(o)
    out: List[Dict[str, Any]] = []
    for key in order:
        group = buckets[key]
        if len(group) == 1:
            out.append(group[0])
            continue
        if anchor:
            prefer = [
                x for x in group
                if anchor_sku_is_master_in_outfit(x, anchor)
            ]
            candidates = prefer if prefer else group
        else:
            candidates = group
        if rrf:
            chosen = max(candidates, key=lambda o: rrf.get(str(o.get("outfit_id") or ""), 0.0))
        else:
            chosen = _pick_one_outfit_stable(candidates, src)
        out.append(chosen)
    return out


_WEIGHT_LABELS: dict[str, str] = {
    "source_match": "图召回匹配",
    "intent_match": "意图匹配",
    "completeness": "完整度",
    "category_l2_match": "中类匹配",
    "tryon_coverage": "试穿覆盖",
    "color_harmony": "色系和谐",
    "price_match": "预算匹配",
    "diversity": "多样性",
}

_DEFAULT_WEIGHTS: dict[str, float] = {
    "source_match": 0.30,
    "intent_match": 0.18,
    "completeness": 0.13,
    "category_l2_match": 0.12,
    "tryon_coverage": 0.09,
    "color_harmony": 0.08,
    "price_match": 0.05,
    "diversity": 0.05,
}


def _get_outfit_rank_weights() -> tuple[tuple[str, str, float], ...]:
    from backend.config import load_config

    cfg_weights = (load_config().get("recommend") or {}).get("outfit_rank_weights") or {}
    if not isinstance(cfg_weights, dict) or not cfg_weights:
        cfg_weights = _DEFAULT_WEIGHTS
    result: list[tuple[str, str, float]] = []
    for key in _DEFAULT_WEIGHTS:
        weight = float(cfg_weights.get(key, _DEFAULT_WEIGHTS[key]))
        label = _WEIGHT_LABELS.get(key, key)
        result.append((key, label, weight))
    return tuple(result)


def _outfit_diversity_score(outfit: Dict[str, Any]) -> float:
    """评估搭配内单品的品类/色系多样性。

    品类数越多、色系越丰富（但不过度），得分越高。
    返回 0.0 ~ 1.0。
    """
    items = outfit.get("items") or []
    if not items:
        return 0.5

    categories: Set[str] = set()
    colors: Set[str] = set()
    for it in items:
        cat = str(it.get("category_l2") or "").strip()
        if cat:
            categories.add(cat)
        cs = it.get("color_series") or []
        if isinstance(cs, str):
            cs = [cs] if cs else []
        for c in cs:
            c = str(c).strip()
            if c:
                colors.add(c)

    n_cat = len(categories)
    n_clr = len(colors)

    if n_cat <= 1:
        cat_score = 0.3
    elif n_cat == 2:
        cat_score = 0.7
    elif n_cat == 3:
        cat_score = 0.9
    else:
        cat_score = 1.0

    if n_clr <= 1:
        clr_score = 0.3
    elif n_clr == 2:
        clr_score = 0.8
    elif n_clr == 3:
        clr_score = 0.9
    else:
        clr_score = max(0.4, 1.0 - 0.15 * (n_clr - 3))

    return round(0.5 * cat_score + 0.5 * clr_score, 4)


def compute_outfit_rank_breakdown(
    outfit: Dict[str, Any],
    *,
    intent_gender: Optional[str] = None,
    intent_season: Optional[List[str]] = None,
    intent_tags: Optional[List[str]] = None,
    in_source_match: bool = False,
    source_match_raw: float = 1.0,
    budget_max: Optional[float] = None,
) -> dict[str, Any]:
    """计算搭配排序总分与各分项（raw / weighted）。"""
    tags = list(intent_tags or [])
    season = list(intent_season or [])
    src_match = source_match_raw if in_source_match else 0.3
    intent_m = intent_attr_match(
        outfit,
        intent_gender,
        season,
        tags,
    )
    comp = float(outfit.get("outfit_completeness_score") or 0.0)
    tryon = float(outfit.get("tryon_coverage") or 0.0)
    price_total = float(outfit.get("price_total") or 0.0)
    pm = 0.5
    if budget_max and budget_max > 0:
        pm = 1.0 if price_total <= budget_max else max(
            0.0,
            1.0 - (price_total - budget_max) / budget_max,
        )
    clr_h = color_harmony_score(outfit.get("items") or [])
    div = _outfit_diversity_score(outfit)
    cat_l2 = category_l2_match_score(outfit.get("items") or [])
    raw_by_key = {
        "source_match": src_match,
        "intent_match": intent_m,
        "completeness": comp,
        "category_l2_match": cat_l2,
        "tryon_coverage": tryon,
        "color_harmony": clr_h,
        "price_match": pm,
        "diversity": div,
    }
    items: list[dict[str, Any]] = []
    total = 0.0
    for key, label, weight in _get_outfit_rank_weights():
        raw = float(raw_by_key[key])
        weighted = weight * raw
        total += weighted
        items.append(
            {
                "key": key,
                "label": label,
                "weight": weight,
                "raw": round(raw, 4),
                "weighted": round(weighted, 4),
            },
        )
    return {
        "total": round(total, 4),
        "items": items,
    }


def _get_coarse_ranking_method() -> str:
    """Read ``recommend.coarse_ranking_method`` from config (default ``rule``)."""
    from backend.config import load_config

    return str(
        (load_config().get("recommend") or {}).get("coarse_ranking_method", "rule")
    )


def _get_model_score_config() -> dict:
    """Read ``recommend.outfit_model_score`` from config."""
    from backend.config import load_config

    return (load_config().get("recommend") or {}).get("outfit_model_score") or {}


def rank_outfits(
    outfits: List[Dict[str, Any]],
    *,
    intent_gender: Optional[str] = None,
    intent_season: Optional[List[str]] = None,
    intent_tags: Optional[List[str]] = None,
    source_match_ids: Optional[Set[str]] = None,
    source_match_scores: Optional[Dict[str, float]] = None,
    budget_max: Optional[float] = None,
) -> List[Tuple[float, Dict[str, Any]]]:
    """返回 (score, outfit) 降序。

    根据 config ``coarse_ranking_method`` 选择打分方式：
    - ``rule``  → 现有 8 维规则打分
    - ``model`` → 调用 outfit-transformer 模型服务打分，失败时 fallback rule

    source_match_scores: outfit_id → 连续分数（如 RRF 归一化值），
    用于替代原来的二值 source_match 信号。未提供时图召回命中默认 1.0。
    """
    method = _get_coarse_ranking_method()

    if method == "model":
        model_cfg = _get_model_score_config()
        service_url = model_cfg.get("service_url", "http://localhost:8100")
        timeout = float(model_cfg.get("timeout", 5))
        scores_map = model_compatibility_scores(outfits, service_url, timeout)
        if scores_map:
            ranked: List[Tuple[float, Dict[str, Any]]] = []
            for o in outfits:
                oid = str(o.get("outfit_id") or "")
                score = scores_map.get(oid, 0.0)
                o["_rank_breakdown"] = {
                    "total": round(score, 4),
                    "items": [{"key": "model_score", "label": "模型搭配分",
                               "weight": 1.0, "raw": round(score, 4),
                               "weighted": round(score, 4)}],
                }
                o["_ranking_scoring_method"] = "model"
                ranked.append((score, o))
            ranked.sort(key=lambda x: -x[0])
            return ranked
        logger.warning("Model scoring failed, falling back to rule-based ranking")

    # Rule-based scoring (default or fallback).
    src_ids = source_match_ids or set()
    src_scores = source_match_scores or {}
    tags = list(intent_tags or [])
    season = list(intent_season or [])
    ranked_rule: List[Tuple[float, Dict[str, Any]]] = []
    for o in outfits:
        oid = str(o.get("outfit_id") or "")
        in_src = oid in src_ids
        src_raw = src_scores.get(oid, 1.0) if in_src else 0.3
        breakdown = compute_outfit_rank_breakdown(
            o,
            intent_gender=intent_gender,
            intent_season=season,
            intent_tags=tags,
            in_source_match=in_src,
            source_match_raw=src_raw,
            budget_max=budget_max,
        )
        score = float(breakdown["total"])
        ranked_rule.append((score, o))
    ranked_rule.sort(key=lambda x: -x[0])
    return ranked_rule


# ---------------------------------------------------------------------------
# LLM 搭配美学打分
# ---------------------------------------------------------------------------


def _format_outfit_for_llm_scoring(outfit: Dict[str, Any]) -> dict[str, Any]:
    """将一套搭配格式化为 LLM 打分所需的文字描述与图片列表。"""
    oid = str(outfit.get("outfit_id") or "")
    items_info: list[dict[str, Any]] = []
    images: list[str] = []
    for it in outfit.get("items") or []:
        item_desc = {
            "sku_id": it.get("sku_id") or "",
            "title": it.get("title") or "",
            "role": it.get("role") or "",
            "price": it.get("price"),
            "gender": normalize_gender_first(it.get("gender")) or "",
            "season": normalize_season(it.get("season")),
            "color_name": it.get("color_name") or "",
            "category_l1": it.get("category_l1") or "",
            "category_l2": it.get("category_l2") or "",
            "material": it.get("material") or "",
            "series": it.get("series") or "",
            "style_tags": it.get("style_tags") or [],
            "occasion_tags": it.get("occasion_tags") or [],
            "fabric_function": it.get("fabric_function") or [],
        }
        items_info.append(item_desc)
        # 用 tryon_image（白底静物主图）作为送给 ranking_llm 的图片，而非 display_image
        img = it.get("tryon_image") or ""
        if img:
            images.append(img)
    # 注入话术 few-shot 参考
    fewshot = ""
    from backend.config import load_config as _load_cfg
    cfg = _load_cfg().get("recommend") or {}
    if cfg.get("reason_generation_mode", "llm") == "llm":
        from backend.dphs_reason_store import match_outfit_reasons, format_reasons_as_fewshot
        matches = match_outfit_reasons(outfit)
        fewshot = format_reasons_as_fewshot(matches)
    return {
        "outfit_id": oid,
        "name": outfit.get("name") or oid,
        "price_total": outfit.get("price_total"),
        "items": items_info,
        "images": images,
        "_fewshot": fewshot,
    }


def _build_llm_user_content(
    outfit_descs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """构建包含文字和图片的 user message content 块。"""
    parts: list[dict[str, Any]] = []
    text_lines: list[str] = []
    for idx, desc in enumerate(outfit_descs, 1):
        text_lines.append(f"--- 搭配 {idx} (outfit_id: {desc['outfit_id']}) ---")
        text_lines.append(f"名称: {desc['name']}")
        text_lines.append(f"总价: {desc.get('price_total')}")
        for it in desc.get("items") or []:
            text_lines.append(
                f"  - [{it.get('role')}] {it.get('title')} "
                f"({it.get('sku_id')}) ¥{it.get('price')}"
            )
            details: list[str] = []
            if it.get("gender"):
                details.append(f"性别:{it['gender']}")
            if it.get("season"):
                s = it["season"] if isinstance(it["season"], list) else [it["season"]]
                details.append(f"季节:{'/'.join(str(x) for x in s)}")
            if it.get("color_name"):
                details.append(f"颜色:{it['color_name']}")
            if it.get("category_l1") or it.get("category_l2"):
                cat = "/".join(
                    x for x in [it.get("category_l1"), it.get("category_l2")] if x
                )
                details.append(f"品类:{cat}")
            if it.get("material"):
                details.append(f"材质:{it['material']}")
            if it.get("series"):
                details.append(f"系列:{it['series']}")
            if it.get("style_tags"):
                details.append(f"风格:{','.join(str(x) for x in it['style_tags'])}")
            if it.get("occasion_tags"):
                details.append(f"场景:{','.join(str(x) for x in it['occasion_tags'])}")
            if it.get("fabric_function"):
                details.append(f"功能:{','.join(str(x) for x in it['fabric_function'])}")
            if details:
                text_lines.append(f"    {' | '.join(details)}")
        # 追加话术参考（如有）
        fewshot = desc.get("_fewshot") or ""
        if fewshot:
            text_lines.append(fewshot)
        text_lines.append("")
    parts.append({"type": "text", "text": "\n".join(text_lines)})

    for desc in outfit_descs:
        for img_url in desc.get("images") or []:
            if not img_url:
                continue
            if img_url.startswith("data:"):
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": img_url},
                })
            elif img_url.startswith("http"):
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": img_url},
                })
    return parts


def _parse_llm_scores(raw: str) -> dict[str, dict[str, Any]]:
    """解析 LLM 返回的 JSON 打分结果，返回 {outfit_id: {"score": float, "brief": str}}。"""
    m = re.search(r"\{[\s\S]*\}", raw or "")
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in data.get("scores") or []:
        oid = str(item.get("outfit_id") or "")
        score = item.get("score")
        if oid and score is not None:
            result[oid] = {
                "score": max(0.0, min(1.0, float(score))),
                "brief": str(item.get("brief") or ""),
                "reason": str(item.get("reason") or ""),
            }
    return result


def _llm_score_batch(
    outfits: List[Dict[str, Any]],
    model_override: str | None = None,
) -> dict[str, dict[str, Any]]:
    """批量模式：所有搭配一起输入 LLM 打分。"""
    from backend.llm_client import _chat_block  # noqa: PLC0415
    from backend.prompt_loader import load_named_prompt  # noqa: PLC0415

    system = load_named_prompt("ranking_outfit_score")
    descs = [_format_outfit_for_llm_scoring(o) for o in outfits]
    user_content = _build_llm_user_content(descs)

    raw = _chat_block(
        "ranking_llm",
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        temperature=0.1,
        model_override=model_override,
    )
    scores = _parse_llm_scores(raw)
    if not scores:
        logger.warning("llm_score_batch: failed to parse scores, fallback to 0.5")
    return scores


def _llm_score_single(
    outfit: Dict[str, Any],
    model_override: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """单套打分：为并行模式服务。"""
    from backend.llm_client import _chat_block  # noqa: PLC0415
    from backend.prompt_loader import load_named_prompt  # noqa: PLC0415

    oid = str(outfit.get("outfit_id") or "")
    system = load_named_prompt("ranking_outfit_score")
    desc = _format_outfit_for_llm_scoring(outfit)
    user_content = _build_llm_user_content([desc])

    raw = _chat_block(
        "ranking_llm",
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        temperature=0.1,
        model_override=model_override,
    )
    scores = _parse_llm_scores(raw)
    return oid, scores.get(oid, {"score": 0.5, "brief": "", "reason": ""})


def _llm_score_parallel(
    outfits: List[Dict[str, Any]],
    max_workers: int = 5,
    model_override: str | None = None,
) -> dict[str, dict[str, Any]]:
    """并行模式：多线程逐套调用 LLM 打分。"""
    scores: dict[str, dict[str, Any]] = {}
    workers = min(max_workers, len(outfits))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_llm_score_single, o, model_override): o
            for o in outfits
        }
        for fut in as_completed(futures):
            try:
                oid, info = fut.result()
                scores[oid] = info
            except Exception:
                logger.exception("llm_score_parallel: single outfit scoring failed")
                o = futures[fut]
                fallback_oid = str(o.get("outfit_id") or "")
                if fallback_oid:
                    scores[fallback_oid] = {"score": 0.5, "brief": "", "reason": ""}
    return scores


def llm_rank_outfits(
    outfits: List[Dict[str, Any]],
    *,
    mode: str = "batch",
    max_workers: int = 5,
    model_override: str | None = None,
) -> List[Tuple[float, Dict[str, Any]]]:
    """使用 LLM 对搭配进行美学打分并排序，返回 (score, outfit) 降序。

    Args:
        outfits: 待排序搭配列表。
        mode: "batch" 批量打分 | "parallel" 多线程逐套打分。
        max_workers: parallel 模式最大线程数。
    """
    if not outfits:
        return []

    if mode == "parallel":
        scores_map = _llm_score_parallel(outfits, max_workers=max_workers, model_override=model_override)
    else:
        scores_map = _llm_score_batch(outfits, model_override=model_override)

    ranked: List[Tuple[float, Dict[str, Any]]] = []
    # template 模式下，reason 用话术库直出，不依赖 LLM 生成的 reason
    from backend.config import load_config as _load_cfg
    use_template_reason = (_load_cfg().get("recommend") or {}).get("reason_generation_mode") == "template"
    for o in outfits:
        oid = str(o.get("outfit_id") or "")
        info = scores_map.get(oid, {"score": 0.5, "brief": "", "reason": ""})
        score = float(info["score"])
        # 将 LLM 打分信息附加到 outfit 上，供下游构建 breakdown
        o["_llm_score"] = score
        o["_llm_brief"] = info.get("brief") or ""
        if use_template_reason:
            from backend.dphs_reason_store import build_template_reason
            o["_llm_reason"] = build_template_reason(o)
        else:
            o["_llm_reason"] = info.get("reason") or ""
        ranked.append((score, o))

    ranked.sort(key=lambda x: -x[0])
    return ranked
