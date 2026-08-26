"""图片理解：VLM + 规则兜底。"""

from __future__ import annotations

from typing import Optional

from backend.embedding_client import embed_image_base64
from backend.llm_client import understand_image_json
from backend.models import ImageUnderstandingResult


def understand_user_image(
    image_base64: Optional[str],
    mime: str = "image/jpeg",
) -> ImageUnderstandingResult:
    if not image_base64:
        return ImageUnderstandingResult()
    raw = understand_image_json(image_base64, mime=mime)
    if not raw:
        return ImageUnderstandingResult(
            image_kind="unknown",
            confidence=0.0,
        )
    return ImageUnderstandingResult(
        image_kind=str(raw.get("image_kind") or "unknown"),
        anchor_role=raw.get("anchor_role"),
        colors=list(raw.get("colors") or []),
        style_tags=list(raw.get("style_tags") or []),
        confidence=float(raw.get("confidence") or 0.0),
        raw=raw,
    )


def image_query_embedding(
    image_base64: Optional[str],
    mime: str = "image/jpeg",
) -> Optional[list[float]]:
    if not image_base64:
        return None
    return embed_image_base64(image_base64, mime=mime)
