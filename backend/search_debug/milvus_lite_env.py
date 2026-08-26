"""Milvus Lite（*.db）连接前暂存 URI，避免 import pymilvus 时解析本地路径异常。"""

from __future__ import annotations

import json
import os


_STASH_KEY = "_OUTFIT_REC_SEARCH_MILVUS_URI_STASH"


def is_milvus_lite_db_uri(uri: str) -> bool:
    u = (uri or "").strip()
    return bool(u) and u.rstrip("/").lower().endswith(".db")


def stash_milvus_db_uri_before_pymilvus_import(*, env_key: str = "MILVUS_URI") -> None:
    name = (env_key or "MILVUS_URI").strip() or "MILVUS_URI"
    raw = (os.environ.get(name) or "").strip()
    if not raw or not is_milvus_lite_db_uri(raw):
        return
    os.environ[_STASH_KEY] = json.dumps({"key": name, "value": raw})
    os.environ[name] = "http://127.0.0.1:19530"


def restore_stashed_milvus_uri() -> None:
    raw = os.environ.pop(_STASH_KEY, None)
    if not raw:
        return
    pair = json.loads(raw)
    os.environ[str(pair["key"])] = str(pair["value"])