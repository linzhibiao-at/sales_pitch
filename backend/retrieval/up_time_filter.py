"""全局上架时间过滤：ES / Milvus 召回 up_time >= 动态下限日期（可按 config 开关）。

开关与窗口由 ``config.yaml`` 的 ``recommend`` 段控制：

- ``enable_up_time_filter`` (bool, 默认 true)：总开关。
    - true → 实时计算下限日期 ``today(UTC) - up_time_filter_days``，常驻过滤。
    - false → 不过滤，全量召回（build_* 返回 None）。
- ``up_time_filter_days`` (int, 默认 180)：滚动窗口天数。

与 ETL 侧 ``scripts/etl_common.MIN_UP_TIME`` / ``up_time_to_epoch`` 对齐口径
（ETL 侧 2023-01-01 决定入索引商品；召回阶段在此之上按近 N 天进一步收紧）：
- ES ``up_time`` 为 date 字段（``yyyy-MM-dd HH:mm:ss||yyyy-MM-dd``），用 range gte 字符串。
- Milvus ``up_time`` 为 INT64 epoch 秒（UTC），用同一 UTC 阈值比较。
"""

from __future__ import annotations

import datetime

# 默认值（config 缺失时的回退）
_DEFAULT_ENABLED = True
_DEFAULT_DAYS = 180


def _load_up_time_cfg() -> tuple[bool, int]:
    """读 config 的开关与窗口天数；缺失回退默认。"""
    from backend.config import load_config

    rec = (load_config() or {}).get("recommend") or {}
    enabled = bool(rec.get("enable_up_time_filter", _DEFAULT_ENABLED))
    try:
        days = int(rec.get("up_time_filter_days", _DEFAULT_DAYS))
    except (TypeError, ValueError):
        days = _DEFAULT_DAYS
    if days < 0:
        days = _DEFAULT_DAYS
    return enabled, days


def _resolve_since() -> str:
    """上架时间下限（yyyy-MM-dd，UTC）。

    - 过滤开启 → 当前 UTC 日期减配置窗口天数。
    - 过滤关闭 → 返回空串（调用方据此跳过）。
    """
    enabled, days = _load_up_time_cfg()
    if not enabled:
        return ""
    today = datetime.datetime.now(datetime.timezone.utc).date()
    return (today - datetime.timedelta(days=days)).strftime("%Y-%m-%d")


def _since_to_epoch(since: str) -> int | None:
    """yyyy-MM-dd（UTC 00:00:00）→ epoch 秒；空串 → None。"""
    if not since:
        return None
    y, m, d = map(int, since.split("-"))
    return int(datetime.datetime(y, m, d, tzinfo=datetime.timezone.utc).timestamp())


def build_up_time_es_filter() -> dict | None:
    """ES up_time range 过滤子句，并入 SKU 检索 bool.filter；关闭时返回 None。"""
    since = _resolve_since()
    if not since:
        return None
    return {"range": {"up_time": {"gte": since}}}


def build_up_time_milvus_expr() -> str | None:
    """Milvus up_time 过滤 expr，并入 SKU 向量召回 expr；关闭时返回 None。"""
    epoch = _since_to_epoch(_resolve_since())
    if epoch is None:
        return None
    return f"up_time >= {epoch}"
