"""离线索引同步状态：支撑 Elasticsearch / Milvus 全量与增量更新。

思路：
  - 用稳定序列化后的文档内容计算 SHA256，记入 data/logs/fila_index_sync_state.json。
  - 增量：仅当哈希相对上次变化时才写入索引（ES bulk / Milvus upsert）。
  - 全量：--reset 清空索引或集合后重灌；或不走增量时仍可全量 upsert（ES/Milvus）。
  - 孤立清理：源 JSONL 中已删除的 id，可从 ES/Milvus 与 state 中移除（--prune-orphans）。

Milvus 向量是否需重算：仅依赖当前 embedding 逻辑使用的图像 URL（及维度、模型名），
与 backend.embedding_client.embed_image_url 一致。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_PATH = ROOT / "data" / "logs" / "fila_index_sync_state.json"
STATE_VERSION = 3


def _normalize_es_state(raw: Any) -> dict[str, Any]:
    """保证 es 下 skus / outfits 均为 dict，兼容旧状态或 null。"""
    if not isinstance(raw, dict):
        return {"skus": {}, "outfits": {}}
    out = dict(raw)
    out.pop("edges", None)
    for key in ("skus", "outfits"):
        val = out.get(key)
        if not isinstance(val, dict):
            out[key] = {}
    return out


def _normalize_milvus_state(raw: Any) -> dict[str, Any]:
    """保证 milvus 下向量桶为 dict。"""
    if not isinstance(raw, dict):
        return {"sku_vectors": {}, "sku_text_vectors": {}, "sku_hybrid_vectors": {}}
    out = dict(raw)
    out.pop("outfit_vectors", None)
    for key in ("sku_vectors", "sku_text_vectors", "sku_hybrid_vectors"):
        val = out.get(key)
        if not isinstance(val, dict):
            out[key] = {}
    return out


def doc_hash(doc: Mapping[str, Any]) -> str:
    """对映射做稳定序列化后取 SHA256（十六进制）。"""
    blob = json.dumps(
        doc,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def milvus_row_signature(
    index_images: list[str],
    *,
    dimensions: int,
    embedding_model: str,
) -> str:
    """决定向量是否需重算：图像 URL 列表 + 维度 + 模型名。"""
    payload = {
        "dimensions": dimensions,
        "model": embedding_model,
        "index_images": sorted(
            (u or "").strip() for u in (index_images or []) if (u or "").strip()
        ),
    }
    return doc_hash(payload)


def milvus_text_row_signature(
    search_text: str,
    *,
    dimensions: int,
    embedding_model: str,
) -> str:
    """决定文本向量是否需重算：search_text + 维度 + 模型名。"""
    payload = {
        "dimensions": dimensions,
        "model": embedding_model,
        "search_text": (search_text or "").strip(),
    }
    return doc_hash(payload)


def load_state(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_STATE_PATH
    if not p.is_file():
        return {
            "version": STATE_VERSION,
            "last_catalog_sync_at": None,
            "es": _normalize_es_state({}),
            "milvus": _normalize_milvus_state({}),
        }
    with p.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {
            "version": STATE_VERSION,
            "last_catalog_sync_at": None,
            "es": _normalize_es_state({}),
            "milvus": _normalize_milvus_state({}),
        }
    data.setdefault("version", STATE_VERSION)
    data.setdefault("last_catalog_sync_at", None)
    data["es"] = _normalize_es_state(data.get("es"))
    data["milvus"] = _normalize_milvus_state(data.get("milvus"))
    return data


def save_state(state: dict[str, Any], path: Path | None = None) -> None:
    p = path or DEFAULT_STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    state["version"] = STATE_VERSION
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(p)


def clear_es_bucket(state: dict[str, Any], bucket: str) -> None:
    if bucket in state.get("es", {}):
        state["es"][bucket] = {}


def clear_milvus_bucket(state: dict[str, Any], bucket: str) -> None:
    if bucket in state.get("milvus", {}):
        state["milvus"][bucket] = {}
