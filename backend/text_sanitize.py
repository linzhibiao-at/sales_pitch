"""向量化前文本清洗（技术方案 §9.1.1）。"""

from __future__ import annotations

import re
from typing import Iterable


def sanitize_text_for_embedding(
    text: str,
    *,
    sku_ids: Iterable[str] | None = None,
    spu_ids: Iterable[str] | None = None,
) -> str:
    """弱化货号/款号与噪声编码，保留可读语义。"""
    s = (text or "").strip()
    for sid in sku_ids or ():
        s = s.replace(str(sid), " ")
    for pid in spu_ids or ():
        s = s.replace(str(pid), " ")
    s = re.sub(r"\b[A-Z0-9]{2,}-[A-Z0-9]{2,}\b", " ", s)
    s = re.sub(r"\b\d{5,}\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,;，；")
    return s
