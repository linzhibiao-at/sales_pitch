"""读取 ``config.yaml``；密钥等仍走环境变量（见 yaml 内 ``*_env``）。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_PATH = _ROOT / "config.yaml"

# ---------- mtime-based config cache ----------
_config_cache: dict[str, Any] | None = None
_config_mtime: float = 0.0
_config_cached_path: Path | None = None


def get_root() -> Path:
    return _ROOT


def load_config(path: Optional[Path] = None) -> dict[str, Any]:
    """Load config.yaml with mtime-based cache.

    Repeated calls within the same process return the cached dict
    as long as the file's mtime has not changed, avoiding repeated
    disk I/O and YAML parsing.
    """
    global _config_cache, _config_mtime, _config_cached_path
    cfg_path = path or _CONFIG_PATH
    try:
        mt = cfg_path.stat().st_mtime
    except OSError:
        mt = 0.0
    if (
        _config_cache is not None
        and mt == _config_mtime
        and cfg_path == _config_cached_path
    ):
        return _config_cache
    with cfg_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    _config_cache = data
    _config_mtime = mt
    _config_cached_path = cfg_path
    return data


def invalidate_config_cache() -> None:
    """Force the next load_config() call to re-read from disk."""
    global _config_cache, _config_mtime, _config_cached_path
    _config_cache = None
    _config_mtime = 0.0
    _config_cached_path = None


def env_or_empty(name: str) -> str:
    return os.environ.get(name, "") or ""


def get_allowed_app_ids(cfg: Optional[dict[str, Any]] = None) -> list[str] | None:
    """对外接口 ``app_id`` 白名单。

    来自顶层 ``allowed_app_ids``（列表）。**键缺失返回 None 表示不强制**，
    保持向后兼容；键存在（即便空列表）则强制校验，非白名单 app_id 返回 401。
    """
    data = cfg if cfg is not None else load_config()
    if "allowed_app_ids" not in data:
        return None
    raw = data.get("allowed_app_ids")
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    return [str(x).strip() for x in raw if str(x).strip()]


# ---------- auth (API Key 鉴权 + 限流) ----------
def get_auth_config(cfg: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """读取 ``config.yaml`` 的 ``auth`` 段。

    返回归一化后的 dict: ``{enabled, header_name, keys_file, log_only,
    rate_limit:{default_qpm, default_daily, default_concurrent,
    default_queue_size, default_queue_timeout}}``。键缺失时 enabled 默认 False
    （向后兼容, 不强制鉴权）。
    """
    data = cfg if cfg is not None else load_config()
    a = data.get("auth") or {}
    rl = a.get("rate_limit") or {}
    return {
        "enabled": bool(a.get("enabled", False)),
        "header_name": str(a.get("header_name") or "X-API-Key"),
        "keys_file": str(a.get("keys_file") or "config/api_keys.yaml"),
        "log_only": bool(a.get("log_only", False)),
        "rate_limit": {
            "default_qpm": int(rl.get("default_qpm") or 100),
            "default_daily": int(rl.get("default_daily") or 10000),
            "default_concurrent": int(rl.get("default_concurrent") or 5),
            "default_queue_size": int(rl.get("default_queue_size") or 20),
            "default_queue_timeout": int(rl.get("default_queue_timeout") or 30),
        },
    }


# api_keys.yaml 的 mtime 缓存(独立于 config.yaml)
_api_keys_cache: list[dict[str, Any]] | None = None
_api_keys_mtime: float = 0.0
_api_keys_path: Path | None = None


def _api_keys_file_path(cfg: Optional[dict[str, Any]] = None) -> Path:
    auth = get_auth_config(cfg)
    rel = auth["keys_file"]
    p = _ROOT / rel
    return p


def load_api_keys(cfg: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """加载 ``auth.keys_file`` 中的 API Key 列表（mtime 缓存, 热加载）。

    文件不存在或解析失败 → 返回空列表（不抛异常, 鉴权降级为拒绝所有 Key）。
    """
    global _api_keys_cache, _api_keys_mtime, _api_keys_path
    p = _api_keys_file_path(cfg)
    try:
        mt = p.stat().st_mtime
    except OSError:
        # 文件不存在: 清缓存返回空
        _api_keys_cache = []
        _api_keys_mtime = 0.0
        _api_keys_path = None
        return []
    if _api_keys_cache is not None and mt == _api_keys_mtime and p == _api_keys_path:
        return _api_keys_cache
    try:
        with p.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        _api_keys_cache = []
        _api_keys_mtime = mt
        _api_keys_path = p
        return []
    raw = data.get("keys") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        raw = []
    keys = [k for k in raw if isinstance(k, dict)]
    _api_keys_cache = keys
    _api_keys_mtime = mt
    _api_keys_path = p
    return keys


# ---------- Redis（DeepAgent 记忆 / checkpointer） ----------
def get_redis_host(cfg: Optional[dict[str, Any]] = None) -> str:
    """Redis 主机：优先环境变量 ``REDIS_HOST``，否则 ``config.yaml`` 的 ``redis.host``。"""
    raw = (os.environ.get("REDIS_HOST") or "").strip()
    if raw:
        return raw
    data = cfg if cfg is not None else load_config()
    return str((data.get("redis") or {}).get("host") or "localhost")


def get_redis_port(cfg: Optional[dict[str, Any]] = None) -> int:
    """Redis 端口：优先环境变量 ``REDIS_PORT``，否则 ``config.yaml`` 的 ``redis.port``。"""
    raw = (os.environ.get("REDIS_PORT") or "").strip()
    if raw:
        return int(raw)
    data = cfg if cfg is not None else load_config()
    return int((data.get("redis") or {}).get("port") or 6379)


def get_redis_db(cfg: Optional[dict[str, Any]] = None) -> int:
    """Redis DB 编号：``config.yaml`` 的 ``redis.db``，默认 0。"""
    data = cfg if cfg is not None else load_config()
    return int((data.get("redis") or {}).get("db") or 0)


def get_agent_resource_dir(cfg: Optional[dict[str, Any]] = None) -> Path:
    """Agent 资源目录（.sales_pitch/）的绝对路径。"""
    data = cfg if cfg is not None else load_config()
    rel = str((data.get("agent") or {}).get("resource_dir") or ".sales_pitch")
    return _ROOT / rel


def get_summarization_config(cfg: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """上下文压缩配置（SummarizationMiddleware）。"""
    data = cfg if cfg is not None else load_config()
    mcfg = (data.get("models") or {}).get("sales_pitch_llm") or {}
    sc = mcfg.get("summarization") or {}
    return {
        "model": str(sc.get("model") or "qwen-turbo"),
        "trigger_tokens": int(sc.get("trigger_tokens") or 50000),
        "keep_messages": int(sc.get("keep_messages") or 10),
    }


# ---------- MySQL（请求审计落库） ----------
def get_mysql_url(cfg: Optional[dict[str, Any]] = None) -> str:
    """MySQL 连接串：优先 ``MYSQL_URL`` 环境变量，否则 ``config.yaml`` 的
    ``mysql.url``。
    """
    raw = (os.environ.get("MYSQL_URL") or "").strip()
    if raw:
        return raw
    data = cfg if cfg is not None else load_config()
    return str((data.get("mysql") or {}).get("url") or "")


def get_mysql_table(cfg: Optional[dict[str, Any]] = None) -> str:
    """审计表名：``config.yaml`` 的 ``mysql.table``，默认 ``request_audit``。"""
    data = cfg if cfg is not None else load_config()
    return str((data.get("mysql") or {}).get("table") or "request_audit")


def get_request_audit_enabled(cfg: Optional[dict[str, Any]] = None) -> bool:
    """对外请求审计落库开关（``request_audit.enabled``，缺省 True）。"""
    data = cfg if cfg is not None else load_config()
    ra = data.get("request_audit") or {}
    return bool(ra.get("enabled", True))
