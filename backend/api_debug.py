"""HTTP / LLM 调试日志（对外话术接口的入出参 trace）。"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

from backend.config import load_config
from backend.logging_config import ReadableFormatter

_logger = logging.getLogger("fila_agent.api_io")
_logger.setLevel(logging.INFO)

# Uvicorn 默认 logging 配置不为 root 挂 StreamHandler；root 的 lastResort
# 仅 WARNING+，导致本模块的 INFO 日志在控制台不可见。
_STDERR_HANDLER_ATTR = "_fila_api_io_stderr_handler"


def _ensure_api_io_stderr_handler() -> None:
    for handler in _logger.handlers:
        if getattr(handler, _STDERR_HANDLER_ATTR, False):
            return
    stream_handler = logging.StreamHandler(sys.stderr)
    setattr(stream_handler, _STDERR_HANDLER_ATTR, True)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(ReadableFormatter())
    _logger.addHandler(stream_handler)
    _logger.propagate = False


_ensure_api_io_stderr_handler()


def _env_flag(names: tuple[str, ...]) -> bool | None:
    for name in names:
        raw = os.environ.get(name, "").strip().lower()
        if raw in ("1", "true", "yes", "on"):
            return True
        if raw in ("0", "false", "no", "off"):
            return False
    return None


def debug_api_io_enabled() -> bool:
    env = _env_flag(("FILA_AGENT_DEBUG_API_IO",))
    if env is not None:
        return env
    cfg = load_config()
    log_cfg = cfg.get("logging") or {}
    return bool(log_cfg.get("debug_api_io"))


def _redact_rules() -> dict[str, Any]:
    cfg = load_config()
    return (cfg.get("logging") or {}).get("redact") or {}


def redact_for_log(obj: Any, depth: int = 0) -> Any:
    if depth > 12:
        return "<max_depth>"
    rules = _redact_rules()
    max_chars = int(rules.get("prompt_max_chars") or 2000)
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k == "api_key" and isinstance(v, str) and v:
                out[k] = "<redacted>"
            else:
                out[k] = redact_for_log(v, depth + 1)
        return out
    if isinstance(obj, list):
        if len(obj) > 50:
            head = [redact_for_log(x, depth + 1) for x in obj[:50]]
            return head + [f"<... {len(obj) - 50} more>"]
        return [redact_for_log(x, depth + 1) for x in obj]
    if isinstance(obj, str) and len(obj) > max_chars:
        return obj[:max_chars] + f"<... len={len(obj)}>"
    return obj


def summarize_http_response(path: str, data: Any) -> dict[str, Any]:
    """HTTP 出参摘要（话术正文截断，避免日志膨胀）。"""
    if not isinstance(data, dict):
        return {"path": path, "kind": type(data).__name__}
    out: dict[str, Any] = {
        "path": path,
        "keys": list(data.keys()),
    }
    pitch = data.get("pitch")
    if isinstance(pitch, str):
        out["pitch_len"] = len(pitch)
        out["pitch_preview"] = pitch[:200]
    return out


def log_flow(tag: str, payload: dict[str, Any]) -> None:
    if not debug_api_io_enabled():
        return
    line = json.dumps({"tag": tag, **payload}, ensure_ascii=False, default=str)
    _logger.info("%s", line)


def summarize_messages_for_llm(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, str):
            s = content
            if len(s) > 400:
                s = s[:400] + f"<... len={len(content)}>"
            out.append({"role": role, "content": s})
        else:
            out.append({"role": role, "content": type(content).__name__})
    return out
