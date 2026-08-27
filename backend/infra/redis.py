"""Redis 基础设施初始化：checkpointer（短期记忆）+ store（长期记忆）。

参考 ``50-DeepAgent/07-CompositeBackendPlusMax.py`` 的 ``init_redis()``。
DeepAgent Harness 使用 LangGraph 的 RedisSaver 做对话 checkpoint，
RedisStore 做文件存储（skills / memory / soul 等）。
"""

from __future__ import annotations

import logging
from typing import Any, Tuple

from backend.config import get_redis_host, get_redis_port, get_redis_db

logger = logging.getLogger(__name__)

# DeepAgent StoreBackend 的固定命名空间
NAMESPACE: tuple[str, ...] = ("fileSystem",)


def init_redis() -> Tuple[Any, Any, Any, Any]:
    """初始化 Redis 连接、checkpointer、store、store_backend。

    Returns:
        (redis_client, checkpointer, store, store_backend)

    Raises:
        ConnectionError: Redis 不可达时向上抛出，由调用方决定是否降级。
    """
    import redis
    from langgraph.checkpoint.redis import RedisSaver
    from langgraph.store.redis import RedisStore
    from deepagents.backends import StoreBackend

    host = get_redis_host()
    port = get_redis_port()
    db = get_redis_db()
    logger.info("[infra] Redis 连接 %s:%s/%s", host, port, db)

    redis_client = redis.Redis(host=host, port=port, db=db)
    # 验证连通性
    redis_client.ping()

    checkpointer = RedisSaver(
        redis_client=redis_client, checkpoint_prefix="sp_checkpointer",
    )
    checkpointer.setup()

    store = RedisStore(conn=redis_client, store_prefix="sp_store")
    store.setup()

    store_backend = StoreBackend(
        store=store, namespace=lambda _rt: NAMESPACE,
    )

    logger.info("[infra] Redis checkpointer + store 初始化完成")
    return redis_client, checkpointer, store, store_backend
