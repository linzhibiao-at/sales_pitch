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


# ---------- Elasticsearch（仅请求审计落库） ----------
def get_elasticsearch_hosts(cfg: Optional[dict[str, Any]] = None) -> list[str]:
    """ES 节点：优先环境变量 ``ES_HOSTS``（逗号分隔），否则用 ``config.yaml`` 的
    ``elasticsearch.hosts``。
    """
    raw = (os.environ.get("ES_HOSTS") or "").strip()
    if raw:
        return [h.strip() for h in raw.split(",") if h.strip()]
    data = cfg if cfg is not None else load_config()
    es_cfg = data.get("elasticsearch") or {}
    hosts = es_cfg.get("hosts") or ["http://127.0.0.1:9200"]
    if isinstance(hosts, str):
        h = hosts.strip()
        return [h] if h else ["http://127.0.0.1:9200"]
    if not hosts:
        return ["http://127.0.0.1:9200"]
    return list(hosts)


def get_elasticsearch_indices(
    cfg: Optional[dict[str, Any]] = None,
) -> dict[str, str]:
    """ES 索引名，仅来自 ``config.yaml`` 的 ``elasticsearch.indices``。

    仅 ``requests``（请求审计）为必需键；未配置或为空时返回空 dict，
    审计写入静默降级。
    """
    data = cfg if cfg is not None else load_config()
    idx = (data.get("elasticsearch") or {}).get("indices") or {}
    out: dict[str, str] = {}
    requests = str(idx.get("requests") or "").strip()
    if requests:
        out["requests"] = requests
    return out


def get_request_audit_enabled(cfg: Optional[dict[str, Any]] = None) -> bool:
    """对外请求审计落库开关（elasticsearch.request_audit.enabled，缺省 True）。"""
    data = cfg if cfg is not None else load_config()
    es = data.get("elasticsearch") or {}
    ra = es.get("request_audit") or {}
    return bool(ra.get("enabled", True))


def elasticsearch_major_version() -> int:
    """已安装 elasticsearch-py 主版本号（7 或 8）。"""
    try:
        import elasticsearch

        ver = elasticsearch.__version__
        if isinstance(ver, tuple):
            return int(ver[0])
        text = str(ver).strip()
        if text.startswith("("):
            return int(text.strip("()").split(",")[0].strip())
        return int(text.split(".")[0])
    except Exception:
        return 7


def _disable_elasticsearch_product_check() -> None:
    """ES 7.x OSS（build_flavor=oss）会触发 7.14+ 客户端的产品校验失败。"""
    try:
        from elasticsearch import transport as es_transport

        checker = es_transport._ProductChecker

        @classmethod
        def _always_success(cls, headers, response):  # noqa: ARG001
            return checker.SUCCESS

        checker.check_product = _always_success
    except Exception:
        pass


def create_elasticsearch_client(
    hosts: list[str],
    *,
    username: str = "",
    password: str = "",
    timeout_sec: int = 30,
) -> Any:
    """构造 ES7 客户端；项目固定使用 elasticsearch-py 7.x（ES 7.9）。"""
    from elasticsearch import Elasticsearch

    _disable_elasticsearch_product_check()
    # 阿里云 ES HTTPS 在容器内常缺中间 CA；临时关闭校验（等同 curl -k）
    kw: dict[str, Any] = {
        "timeout": timeout_sec,
        "verify_certs": False,
        "ssl_show_warn": False,
    }
    if username:
        kw["http_auth"] = (username, password)
    return Elasticsearch(hosts, **kw)
