"""读取 ``fila_agent_html/config.yaml``；密钥等仍走环境变量（见 yaml 内 ``*_env``）。"""

from __future__ import annotations

import datetime
import json
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
    disk I/O and YAML parsing (~20× per request in hot path).
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


def _recommend_cfg(cfg: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    data = cfg if cfg is not None else load_config()
    return data.get("recommend") or {}


def recall_outfit_limit(cfg: Optional[dict[str, Any]] = None) -> int:
    """召回拼套阶段每路最多产出搭配套数。"""
    rec = _recommend_cfg(cfg)
    if rec.get("recall_outfit_limit") is not None:
        return int(rec["recall_outfit_limit"])
    return int(rec.get("default_outfit_limit") or 50)


def rank_outfit_limit(cfg: Optional[dict[str, Any]] = None) -> int:
    """精排后最终返回给用户的搭配套数。"""
    rec = _recommend_cfg(cfg)
    if rec.get("rank_outfit_limit") is not None:
        return int(rec["rank_outfit_limit"])
    return int(rec.get("default_outfit_limit") or 5)


def get_up_time_since(cfg: Optional[dict[str, Any]] = None) -> str:
    """召回阶段全局上架时间下限（yyyy-MM-dd，UTC 口径）；空串表示不过滤。

    实时计算：当前 UTC 日期减 ``recommend.up_time_filter_days`` 天（默认 180）。
    当 ``recommend.enable_up_time_filter`` 为 false（或留空禁用）时返回空串。
    供 ES/Milvus 的 up_time 过滤共用。
    """
    rec = _recommend_cfg(cfg)
    if not bool(rec.get("enable_up_time_filter", True)):
        return ""
    try:
        days = int(rec.get("up_time_filter_days", 180))
    except (TypeError, ValueError):
        days = 180
    if days < 0:
        days = 180
    today = datetime.datetime.now(datetime.timezone.utc).date()
    return (today - datetime.timedelta(days=days)).strftime("%Y-%m-%d")


def get_allowed_app_ids(cfg: Optional[dict[str, Any]] = None) -> list[str] | None:
    """对外接口 ``app_id`` 白名单（ISS-04）。

    来自 ``recommend.allowed_app_ids``（列表）。**键缺失返回 None 表示不强制**，
    保持向后兼容；键存在（即便空列表）则强制校验，非白名单 app_id 返回 401。
    """
    rec = _recommend_cfg(cfg)
    if "allowed_app_ids" not in rec:
        return None
    raw = rec.get("allowed_app_ids")
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
    """ES 索引名，仅来自 ``config.yaml`` 的 ``elasticsearch.indices``。"""
    data = cfg if cfg is not None else load_config()
    idx = (data.get("elasticsearch") or {}).get("indices") or {}
    keys = ("skus", "outfits")
    missing = [k for k in keys if not str(idx.get(k) or "").strip()]
    if missing:
        raise ValueError(
            "config.yaml elasticsearch.indices 未配置完整，缺少: "
            + ", ".join(missing),
        )
    out = {k: str(idx[k]).strip() for k in keys}
    reviews = str(idx.get("reviews") or "").strip()
    if reviews:
        out["reviews"] = reviews
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


def get_elasticsearch_index(
    name: str,
    cfg: Optional[dict[str, Any]] = None,
) -> str:
    """单个 ES 索引名（``skus`` / ``outfits``）。"""
    return get_elasticsearch_indices(cfg)[name]


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


def get_milvus_mode(cfg: Optional[dict[str, Any]] = None) -> str:
    """Milvus 连接模式：``local``（Milvus Lite）或 ``cloud``（托管集群）。"""
    data = cfg if cfg is not None else load_config()
    mv = data.get("milvus") or {}
    mode_env = str(mv.get("mode_env") or "FILA_MILVUS_MODE")
    raw = (os.environ.get(mode_env) or mv.get("mode") or "local").strip()
    mode = raw.lower()
    if mode not in ("local", "cloud"):
        raise ValueError(
            f"无效 Milvus mode={raw!r}，仅支持 local | cloud",
        )
    return mode


def get_milvus_uri(cfg: Optional[dict[str, Any]] = None) -> str:
    """Milvus URI：优先 ``uri_env`` 环境变量，否则按 ``mode`` 选本地或云端。"""
    data = cfg if cfg is not None else load_config()
    mv = data.get("milvus") or {}
    env_key = str(mv.get("uri_env") or "FILA_MILVUS_URI")
    uri = (os.environ.get(env_key) or "").strip()
    if uri:
        return uri
    mode = get_milvus_mode(data)
    if mode == "cloud":
        cloud = mv.get("cloud") or {}
        cloud_uri = str(cloud.get("uri") or "").strip()
        if cloud_uri:
            return cloud_uri
        return ""
    rel = (mv.get("local_data_file") or "").strip()
    if not rel:
        return ""
    path = (_ROOT / rel).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def get_milvus_token(cfg: Optional[dict[str, Any]] = None) -> str:
    """Milvus 认证 token：优先 ``token_env``；cloud 模式可拼 ``username:password``。"""
    data = cfg if cfg is not None else load_config()
    mv = data.get("milvus") or {}
    token_key = str(mv.get("token_env") or "FILA_MILVUS_TOKEN")
    token = (os.environ.get(token_key) or "").strip()
    if token:
        return token
    if get_milvus_mode(data) != "cloud":
        return ""
    cloud = mv.get("cloud") or {}
    user_env = str(cloud.get("username_env") or "FILA_MILVUS_USERNAME")
    pwd_env = str(cloud.get("password_env") or "FILA_MILVUS_PASSWORD")
    username = (os.environ.get(user_env) or cloud.get("username") or "").strip()
    password = (os.environ.get(pwd_env) or "").strip()
    if username and password:
        return f"{username}:{password}"
    return ""


def is_milvus_lite_local_uri(uri: str) -> bool:
    if not (uri or "").strip():
        return False
    return uri.rstrip("/").lower().endswith(".db")


_STASH_MILVUS_LITE_ENV = "_OUTFIT_REC_MILVUS_URI_STASH_JSON"


def stash_milvus_db_uri_before_pymilvus_import(uri_env_name: str) -> None:
    """避免 pymilvus import 时解析到本地 ``*.db``；见 ``restore_stashed_milvus_uri``。

    pymilvus 固定读取 ``MILVUS_URI``，故同时暂存配置项与 ``MILVUS_URI``。
    """
    names: list[str] = []
    for candidate in (uri_env_name or "MILVUS_URI", "MILVUS_URI"):
        key = (candidate or "").strip()
        if key and key not in names:
            names.append(key)
    stashed: list[dict[str, str]] = []
    for name in names:
        raw = (os.environ.get(name) or "").strip()
        if not raw or not is_milvus_lite_local_uri(raw):
            continue
        stashed.append({"key": name, "value": raw})
        os.environ[name] = "http://127.0.0.1:19530"
    if stashed:
        os.environ[_STASH_MILVUS_LITE_ENV] = json.dumps(stashed)


def restore_stashed_milvus_uri() -> None:
    raw = os.environ.pop(_STASH_MILVUS_LITE_ENV, None)
    if not raw:
        return
    parsed = json.loads(raw)
    pairs = parsed if isinstance(parsed, list) else [parsed]
    for pair in pairs:
        os.environ[str(pair["key"])] = str(pair["value"])
