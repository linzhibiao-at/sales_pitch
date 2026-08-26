"""幂等创建评审 ES 索引 umalog-q-maiamgs-index-fila-reviews。

用法:
  python -m scripts.build_fila_reviews_es_index            # 不存在则建,存在则跳过
  python -m scripts.build_fila_reviews_es_index --reset    # 先删再建(清空数据)
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

from backend.config import (
    create_elasticsearch_client,
    env_or_empty,
    get_elasticsearch_hosts,
    get_elasticsearch_index,
    load_config,
)

logger = logging.getLogger(__name__)

REVIEW_INDEX_MAPPING: dict[str, Any] = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
    },
    "mappings": {
        "properties": {
            "data_file": {"type": "keyword"},
            "input_sku_id": {"type": "keyword"},
            "outfit_id": {"type": "keyword"},
            "rating": {"type": "integer"},
            "comment": {"type": "text"},
            "reviewer": {"type": "keyword"},
            "reviewer_role": {"type": "keyword"},
            "reviewer_name": {"type": "keyword"},
            "created_at": {"type": "date", "format": "strict_date_optional_time||iso8601"},
            "updated_at": {"type": "date", "format": "strict_date_optional_time||iso8601"},
        }
    },
}


def build_index(client: Any, name: str, reset: bool = False) -> None:
    """幂等建索引:默认存在则跳过;reset=True 先删再建。"""
    if client.indices.exists(index=name):
        if not reset:
            logger.info("索引 %s 已存在,跳过(如需重建加 --reset)", name)
            return
        logger.info("--reset: 删除旧索引 %s", name)
        client.indices.delete(index=name)
    logger.info("创建索引 %s", name)
    client.indices.create(index=name, body=REVIEW_INDEX_MAPPING)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="创建 FILA 评审 ES 索引")
    parser.add_argument("--reset", action="store_true", help="删除已有索引后重建(清空数据)")
    args = parser.parse_args()

    cfg = load_config()
    name = get_elasticsearch_index("reviews", cfg)
    hosts = get_elasticsearch_hosts(cfg)
    es_cfg = cfg.get("elasticsearch") or {}
    user = env_or_empty(str(es_cfg.get("username_env") or ""))
    pwd = env_or_empty(str(es_cfg.get("password_env") or ""))
    client = create_elasticsearch_client(
        hosts, username=user, password=pwd, timeout_sec=30,
    )
    if not client.ping():
        raise SystemExit("ES ping 失败,检查 ES_HOSTS/ES_USERNAME/ES_PASSWORD")
    build_index(client, name, reset=args.reset)
    logger.info("完成: %s", name)


if __name__ == "__main__":
    main()
