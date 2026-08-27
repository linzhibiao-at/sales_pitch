"""集中式日志配置：可读格式化 + 管线阶段高亮 + 可选彩色输出。

设计目标
--------
* 控制台（stderr）输出对开发 / 调试友好：时间戳、级别、模块一目了然
* 管线阶段日志（``[FILA穿搭管线]``）以 ``key=value`` 形式紧凑展示，
  避免整行 JSON 难以扫读
* ``log_flow`` 的 JSON 负载做 pretty-print（缩进 + 换行），
  便于在终端 grep / tail
* 当 stderr 不是 TTY（如 nohup 重定向到文件）时自动关闭 ANSI 彩色
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any


# ─────────────────────────── 彩色工具 ───────────────────────────

def _supports_color() -> bool:
    """判断 stderr 是否支持 ANSI 转义序列。"""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(sys.stderr, "isatty") and sys.stderr.isatty()


_COLOR_ENABLED = _supports_color()

# ANSI 色码
_RESET = "\033[0m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_GREEN = "\033[32m"
_CYAN = "\033[36m"
_MAGENTA = "\033[35m"
_BLUE = "\033[34m"

_LEVEL_COLORS: dict[int, str] = {
    logging.DEBUG: _DIM,
    logging.INFO: _GREEN,
    logging.WARNING: _YELLOW,
    logging.ERROR: _RED,
    logging.CRITICAL: _BOLD + _RED,
}


def _c(code: str, text: str) -> str:
    """如果终端支持彩色，给 *text* 包裹 ANSI 色码。"""
    if _COLOR_ENABLED:
        return f"{code}{text}{_RESET}"
    return text


# ──────────────────────── 紧凑 KV 格式化 ────────────────────────

def _fmt_value(v: Any, max_len: int = 120) -> str:
    """将值格式化为紧凑字符串；长字符串截断。"""
    if isinstance(v, float):
        return f"{v:.4f}" if abs(v) < 1 else f"{v:.2f}"
    if isinstance(v, (list, dict)):
        s = json.dumps(v, ensure_ascii=False, default=str)
        if len(s) > max_len:
            return s[:max_len] + "…"
        return s
    s = str(v)
    if len(s) > max_len:
        return s[:max_len] + "…"
    return s


def format_kv_pairs(fields: dict[str, Any], *, indent: str = "  ") -> str:
    """将字典渲染为多行 ``key=value`` 块。"""
    if not fields:
        return ""
    lines: list[str] = []
    for k, v in fields.items():
        lines.append(f"{indent}{_c(_CYAN, k)}={_fmt_value(v)}")
    return "\n".join(lines)


# ──────────────────────── 自定义 Formatter ────────────────────────

class ReadableFormatter(logging.Formatter):
    """人类可读的日志格式化器。

    输出示例::

        2026-06-12 10:23:45.123 INFO  [fila_agent.api_io]
          tag=recommend_stage  stage=intent_extract  环节=意图解析
          elapsed_ms=42  since_request_ms=120  method=trie

    对于非管线日志（普通 ``logger.info(...)``），退化为一行::

        2026-06-12 10:23:45.123 INFO  [backend.services.recommend_service]
          [意图解析·图向量近邻] recall_threshold=0.6000, ...
    """

    def __init__(self, *, compact_json: bool = True) -> None:
        super().__init__()
        self._compact_json = compact_json

    # ---- public ----

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        ts = datetime.fromtimestamp(
            record.created, tz=timezone.utc,
        ).astimezone().strftime("%Y-%m-%d %H:%M:%S.") + f"{record.msecs:03.0f}"
        level = record.levelname
        name = record.name
        msg = record.getMessage()

        # 色码
        ts_s = _c(_DIM, ts)
        level_color = _LEVEL_COLORS.get(record.levelno, "")
        level_s = _c(level_color, f"{level:<5}")
        name_s = _c(_DIM, f"[{name}]")

        # 管线阶段日志：提取 [FILA穿搭管线] 前缀后的 JSON 做 KV 展开
        if msg.startswith("[FILA穿搭管线] "):
            return self._format_pipeline(ts_s, level_s, name_s, msg)

        # log_flow 的 tag= 前缀日志也做 KV 展开
        if msg.startswith("{") and '"tag"' in msg[:30]:
            return self._format_flow_json(ts_s, level_s, name_s, msg)

        # 普通日志：保持一行
        return f"{ts_s} {level_s} {name_s} {msg}"

    # ---- private ----

    _STAGE_SEP = "=" * 100  # 管线阶段之间的分隔线

    def _format_pipeline(
        self, ts: str, level: str, name: str, msg: str,
    ) -> str:
        header = f"{ts} {level} {name} {_c(_BOLD + _MAGENTA, '[FILA穿搭管线]')}"
        json_part = msg[len("[FILA穿搭管线] "):]
        fields = self._safe_parse_json(json_part)
        sep = _c(_DIM, self._STAGE_SEP)
        if fields:
            # 把关键排序字段提到前面
            ordered = self._reorder_pipeline_fields(fields)
            body = format_kv_pairs(ordered)
            return f"{sep}\n{header}\n{body}"
        return f"{sep}\n{header} {json_part}"

    def _format_flow_json(
        self, ts: str, level: str, name: str, msg: str,
    ) -> str:
        fields = self._safe_parse_json(msg)
        if fields:
            tag = fields.pop("tag", "flow")
            header = f"{ts} {level} {name} {_c(_BOLD + _BLUE, f'[{tag}]')}"
            body = format_kv_pairs(fields)
            return f"{header}\n{body}"
        return f"{ts} {level} {name} {msg}"

    @staticmethod
    def _safe_parse_json(text: str) -> dict[str, Any] | None:
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass
        return None

    @staticmethod
    def _reorder_pipeline_fields(fields: dict[str, Any]) -> dict[str, Any]:
        """把最常被关注的字段排在前面。"""
        priority = [
            "tag", "trace_id", "stage", "环节",
            "elapsed_ms", "since_request_ms",
            "anchor_sku_id", "outfit_count",
        ]
        ordered: dict[str, Any] = {}
        for k in priority:
            if k in fields:
                ordered[k] = fields[k]
        for k, v in fields.items():
            if k not in ordered:
                ordered[k] = v
        return ordered


# ──────────────────────── 初始化入口 ────────────────────────

_LOGGING_INITIALIZED = False


def setup_logging(
    *,
    level: str | None = None,
    force: bool = False,
) -> None:
    """初始化全局日志：为 ``fila_agent`` 与 ``backend`` 命名空间挂上格式化 handler。

    * 仅在首次调用时生效（除非 ``force=True``），避免重复挂载
    * uvicorn 自身的 logger（``uvicorn.access`` 等）不受影响
    * ``level`` 未指定时从 ``config.yaml → logging.level`` 读取
    """
    global _LOGGING_INITIALIZED
    if _LOGGING_INITIALIZED and not force:
        return
    _LOGGING_INITIALIZED = True

    if level is None:
        try:
            from backend.config import load_config
            level = str(
                (load_config().get("logging") or {}).get("level") or "INFO",
            )
        except Exception:
            level = "INFO"

    fmt = ReadableFormatter()
    log_level = getattr(logging, level.upper(), logging.INFO)

    # 构造 stderr handler
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(fmt)
    handler.setLevel(log_level)

    # fila_agent 命名空间（api_debug 等）
    fila_logger = logging.getLogger("fila_agent")
    fila_logger.handlers.clear()
    fila_logger.addHandler(handler)
    fila_logger.setLevel(log_level)
    fila_logger.propagate = False

    # backend 命名空间（main / auth / services 等业务模块）
    backend_logger = logging.getLogger("backend")
    backend_logger.handlers.clear()
    backend_logger.addHandler(handler)
    backend_logger.setLevel(log_level)
    backend_logger.propagate = False

    # httpx / openai 等第三方库日志降级，减少噪音
    for noisy in ("httpx", "httpcore", "openai", "elasticsearch", "urllib3"):
        lg = logging.getLogger(noisy)
        lg.setLevel(logging.WARNING)
