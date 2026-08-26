"""异步保存用户上传图片到 data/input_images，按 MD5 去重。"""

from __future__ import annotations

import base64
import hashlib
import logging
import threading
from pathlib import Path

from backend.config import get_root

logger = logging.getLogger(__name__)

_SAVE_DIR = get_root() / "data" / "input_images"
_SAVE_DIR.mkdir(parents=True, exist_ok=True)


def _save_image(image_base64: str) -> None:
    """解码 base64 图片，计算 MD5，去重后写入磁盘。"""
    try:
        raw = base64.b64decode(image_base64)
        md5 = hashlib.md5(raw).hexdigest()
        dest = _SAVE_DIR / f"{md5}.jpg"
        if dest.exists():
            logger.debug("图片已存在，跳过: %s", dest.name)
            return
        dest.write_bytes(raw)
        logger.info("已保存用户图片: %s (%d bytes)", dest.name, len(raw))
    except Exception:
        logger.exception("保存用户图片失败")


def save_image_async(image_base64: str | None) -> None:
    """在后台线程中保存图片，不阻塞主流程。"""
    if not image_base64:
        return
    t = threading.Thread(target=_save_image, args=(image_base64,), daemon=True)
    t.start()
