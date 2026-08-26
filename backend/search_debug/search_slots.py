"""从 LLM 意图抽取结果中提取用于 ES must/multi_match 的检索文本。"""

from __future__ import annotations

from typing import Any


def _coerce_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        parts = [str(x).strip() for x in value if str(x).strip()]
        return " ".join(parts)
    s = str(value).strip()
    if s.lower() in ("null", "none"):
        return ""
    return s


def text_for_must(extraction: dict[str, Any], fallback_q: str) -> str:
    """优先 ``keywords``；模型显式置 null 时不再回退整句（避免与 filter 重复）。

    ``keywords`` 字段缺失时依次尝试 ``normalized_text``、用户原始检索词。
    """
    if not extraction:
        return (fallback_q or "").strip()

    if "keywords" in extraction:
        return _coerce_text(extraction.get("keywords"))

    norm = _coerce_text(extraction.get("normalized_text"))
    if norm:
        return norm

    return (fallback_q or "").strip()