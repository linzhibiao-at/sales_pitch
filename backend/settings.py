"""兼容旧 import：统一从 ``backend.config`` 加载。"""

from backend.config import (  # noqa: F401
    create_elasticsearch_client,
    env_or_empty,
    get_elasticsearch_hosts,
    get_elasticsearch_index,
    get_elasticsearch_indices,
    get_milvus_mode,
    get_milvus_token,
    get_milvus_uri,
    get_root,
    is_milvus_lite_local_uri,
    load_config,
    restore_stashed_milvus_uri,
    stash_milvus_db_uri_before_pymilvus_import,
)
