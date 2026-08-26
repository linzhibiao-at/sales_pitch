"""结构化 JSONL 日志与 replay 落盘。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from backend.config import get_root, load_config


def _tz_now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(
        timespec="seconds",
    )


def _utf8_safe(s: str) -> str:
    """把含 lone surrogate 的 str 转成可安全 utf-8 编码的 str。

    防止 message 等字段携带非法 Unicode 时, 写盘(ensure_ascii=False)抛
    UnicodeEncodeError 把整条请求拖垮(ISS-06)。合法字符不受影响。
    """
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        return s.encode("utf-8", "replace").decode("utf-8")


class JsonlLogger:
    """按事件写入 JSONL，失败时降级到 logging。"""

    def __init__(self) -> None:
        self._cfg = load_config()
        log_cfg = self._cfg.get("logging") or {}
        self._base = get_root() / (log_cfg.get("base_dir") or "data/logs")
        self._level = (log_cfg.get("level") or "INFO").upper()
        self._py = logging.getLogger("fila_agent")

    def _path(self, rel: str) -> Path:
        p = self._base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def log(
        self,
        event: str,
        module: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        trace_id: Optional[str] = None,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
        sku_id: Optional[str] = None,
        spu_id: Optional[str] = None,
        outfit_id: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        row = {
            "ts": _tz_now_iso(),
            "level": self._level,
            "event": event,
            "trace_id": trace_id,
            "session_id": session_id,
            "run_id": run_id,
            "module": module,
            "sku_id": sku_id,
            "spu_id": spu_id,
            "outfit_id": outfit_id,
            "message": message or event,
            "payload": payload or {},
        }
        line = json.dumps(row, ensure_ascii=False)
        try:
            day = datetime.now().strftime("%Y%m%d")
            fp = self._path(f"online/recommend_{day}.jsonl")
            with fp.open("a", encoding="utf-8") as f:
                f.write(_utf8_safe(line) + "\n")
        except OSError:
            self._py.info("%s", line)

    def dump_replay(self, trace_id: str, data: Dict[str, Any]) -> None:
        cfg = load_config().get("logging") or {}
        if not cfg.get("enable_replay_dump", True):
            return
        try:
            fp = self._path(f"replay/{trace_id}.json")
            fp.parent.mkdir(parents=True, exist_ok=True)
            with fp.open("w", encoding="utf-8") as f:
                f.write(_utf8_safe(json.dumps(data, ensure_ascii=False, indent=2)))
        except OSError:
            self._py.exception("replay dump failed")
