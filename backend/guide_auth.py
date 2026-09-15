"""导购身份校验（用户级鉴权, mock 用户数据源）。

与 ``backend/auth.py``（应用级 API Key + 限流）互补: ``auth`` 回答
"哪个应用在调用", 本模块回答"哪位导购在操作"。用户数据源暂由
``config.yaml`` 的 ``guide_auth`` 段提供（mock 列表, 暂代数据库 user 表）;
``GuideUserStore.get()`` 为唯一数据访问入口, 未来可平滑替换为
MySQL ``user`` 表查询（见 openspec 变更 add-guide-identity-verification）。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import HTTPException

from backend.config import get_guide_auth_config

logger = logging.getLogger(__name__)


class GuideUserStore:
    """mock 导购用户白名单（热加载 ``config.yaml`` 的 ``guide_auth`` 段）。"""

    def get(self, guide_num: str) -> Optional[dict[str, Any]]:
        """按 guide_num 存在性查询; 未找到返回 None。

        每次调用经 ``get_guide_auth_config()`` 读取（mtime 缓存）,
        改配置即热生效, 不在初始化时缓存用户列表。
        """
        gid = (guide_num or "").strip()
        if not gid:
            return None
        for u in get_guide_auth_config()["users"]:
            if u.get("guide_num") == gid:
                return u
        return None


# 进程级单例
_guide_user_store = GuideUserStore()


def get_guide_user_store() -> GuideUserStore:
    return _guide_user_store


def verify_guide_identity(guide_num: Optional[str]) -> Optional[dict[str, Any]]:
    """导购身份校验（路由处理器内调用, 见 design.md D1/D6）:

    - ``guide_auth.enabled=false`` → 返回 None 放行（向后兼容）
    - ``guide_num`` 缺失/空白 → 400 ``guide_num required``
    - ``guide_num`` 不在用户列表 → 401 ``guide not found``
    - 命中 → 返回用户 dict（调用方可写入 ``request.state.guide``）
    """
    cfg = get_guide_auth_config()
    if not cfg["enabled"]:
        return None
    gid = (guide_num or "").strip()
    if not gid:
        raise HTTPException(status_code=400, detail="guide_num required")
    user = _guide_user_store.get(gid)
    if user is None:
        logger.info("[guide_auth] unknown guide_num: %s", gid)
        raise HTTPException(status_code=401, detail="guide not found")
    return user
