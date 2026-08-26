"""从 prompt/*.md 加载提示词，支持 mtime 缓存（开发态）。"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from backend.config import get_root, load_config

logger = logging.getLogger(__name__)

_CACHE: dict[str, tuple[float, str]] = {}


def _hot_reload_enabled() -> bool:
    return os.environ.get(
        "FILA_AGENT_PROMPT_HOT_RELOAD", "",
    ).lower() in ("1", "true", "yes", "on")


def _resolve_under_prompt_dir(rel: str) -> Path:
    root = get_root()
    rel_norm = rel.replace("\\", "/").lstrip("/")
    p = (root / rel_norm).resolve()
    prompt_root = (root / "prompt").resolve()
    if not str(p).startswith(str(prompt_root)):
        raise ValueError(f"prompt path must stay under prompt/: {rel}")
    return p


def load_prompt_file(rel_path: str) -> str:
    """读取相对 fila_agent_html/ 的 md，须位于 prompt/ 下。"""
    path = _resolve_under_prompt_dir(rel_path)
    if _hot_reload_enabled():
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        hit = _CACHE.get(rel_path)
        if hit and hit[0] == mtime:
            return hit[1]
        text = path.read_text(encoding="utf-8")
        _CACHE[rel_path] = (mtime, text)
        return text
    hit = _CACHE.get(rel_path)
    if hit:
        return hit[1]
    text = path.read_text(encoding="utf-8")
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    _CACHE[rel_path] = (mtime, text)
    return text


def validate_prompt_paths() -> list[str]:
    """启动校验：config 中声明的 prompt 文件均可读。"""
    cfg = load_config()
    errs: list[str] = []
    roots: list[str] = []
    pf = cfg.get("prompt_files") or {}
    if isinstance(pf, dict):
        roots.extend(str(v) for v in pf.values() if v)
    rec = cfg.get("recommend") or {}
    rp = rec.get("reason_prompts") or {}
    if isinstance(rp, dict):
        roots.extend(str(v) for v in rp.values() if v)
    for rel in sorted(set(roots)):
        try:
            p = _resolve_under_prompt_dir(rel)
            if not p.is_file():
                errs.append(f"missing: {rel}")
        except (OSError, ValueError) as e:
            errs.append(f"{rel}: {e}")
    return errs


def load_named_prompt(key: str) -> str:
    """从 prompt_files 或 recommend.reason_prompts 键加载正文。"""
    cfg = load_config()
    pf = cfg.get("prompt_files") or {}
    if key in pf:
        return load_prompt_file(str(pf[key]))
    rec = cfg.get("recommend") or {}
    rp = rec.get("reason_prompts") or {}
    if key in rp:
        return load_prompt_file(str(rp[key]))
    raise KeyError(f"unknown prompt key: {key}")
