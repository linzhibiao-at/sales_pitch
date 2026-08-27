"""Agent 资源加载 + 构建。

参考 ``50-DeepAgent/07-CompositeBackendPlusMax.py`` 的
``load_agents / load_skills / load_souls / build_agent``。

启动时调用 ``load_resources()`` 把 AGENTS.md / SOUL.md / SKILL.md 加载到
RedisStore，然后 ``build_agent()`` 创建带记忆 + 上下文压缩的 Agent。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from backend.config import get_agent_resource_dir, get_summarization_config

logger = logging.getLogger(__name__)

# StoreBackend 的固定命名空间（与 infra/redis.py 保持一致）
NAMESPACE: tuple[str, ...] = ("fileSystem",)


# ── 资源加载 ─────────────────────────────────────────────────────────────

def load_agents(store: Any) -> None:
    """读取 AGENTS.md 存入 Store。"""
    from deepagents.backends.utils import create_file_data

    agent_path = get_agent_resource_dir() / "AGENTS.md"
    if not agent_path.exists():
        logger.warning("[agent] AGENTS.md 不存在: %s", agent_path)
        return
    content = agent_path.read_text(encoding="utf-8")
    store.put(
        namespace=NAMESPACE,
        key="/memory/AGENTS.md",
        value=create_file_data(content),
    )
    logger.info("[agent] AGENTS.md 已存储 (%d 字符)", len(content))


def load_skills(store: Any) -> None:
    """遍历 skills 目录，只加载每个 skill 下的 SKILL.md。"""
    from deepagents.backends.utils import create_file_data

    skills_dir = get_agent_resource_dir() / "skills"
    if not skills_dir.exists():
        logger.warning("[agent] skills 目录不存在: %s", skills_dir)
        return
    for skill_md in sorted(skills_dir.rglob("SKILL.md")):
        relative_path = skill_md.relative_to(skills_dir)
        store_key = f"/skills/{relative_path}"
        content = skill_md.read_text(encoding="utf-8")
        store.put(
            namespace=NAMESPACE,
            key=store_key,
            value=create_file_data(content),
        )
        logger.info("[agent] 已存储: %s (%d 字符)", store_key, len(content))


def load_souls(store: Any) -> dict[str, str]:
    """解析 SOUL.md，将每个话术风格的 name/description 存入 Store。"""
    from deepagents.backends.utils import create_file_data

    soul_path = get_agent_resource_dir() / "SOUL.md"
    if not soul_path.exists():
        logger.warning("[agent] SOUL.md 不存在: %s", soul_path)
        return {}
    soul_content = soul_path.read_text(encoding="utf-8")
    soul_pattern = re.compile(
        r"name:\s*(.+?)\s*\ndescription:\s*(.+?)(?=\n---)", re.DOTALL,
    )
    souls = soul_pattern.findall(soul_content)
    soul_dict: dict[str, str] = {}
    for name, description in souls:
        name = name.strip()
        description = description.strip()
        store.put(
            namespace=NAMESPACE,
            key=f"/soul/{name}",
            value=create_file_data(description),
        )
        soul_dict[name] = description
        logger.info("[agent] SOUL: %s -> %s...", name, description[:50])
    return soul_dict


def load_resources(store: Any) -> dict[str, str]:
    """启动时一次性加载所有 Agent 资源到 Store。

    Returns:
        ``soul_dict``：``{name: description}`` 风格映射。
    """
    load_agents(store)
    load_skills(store)
    return load_souls(store)


# ── Agent 构建 ─────────────────────────────────────────────────────────

def build_agent(
    llm: Any,
    store_backend: Any,
    store: Any,
    checkpointer: Any,
    *,
    soul_name: str | None = None,
) -> Any:
    """创建带记忆 + 上下文压缩的话术 Agent。

    Args:
        llm: LangChain Runnable（双通道 fallback 链）。
        store_backend: DeepAgent StoreBackend。
        store: RedisStore。
        checkpointer: RedisSaver。
        soul_name: 话术风格名（如 ``"warm"``），为 None 时不加载 SOUL。

    Returns:
        DeepAgent CompiledGraph，可直接 ``.ainvoke()``。
    """
    from deepagents import create_deep_agent, FilesystemPermission
    from deepagents.middleware.summarization import SummarizationMiddleware
    from backend.agent.middleware import AlwaysLoadMemoryMiddleware
    from backend.llm.factory import create_summarization_llm

    # 记忆源：AGENTS.md 必须；SOUL 按请求可选
    memory_sources = ["/memory/AGENTS.md"]
    if soul_name:
        memory_sources.append(f"/soul/{soul_name}")

    # 自定义记忆中间件（每次都重新加载）
    memory_middleware = AlwaysLoadMemoryMiddleware(
        backend=store_backend,
        sources=memory_sources,
    )

    # 上下文压缩中间件
    sc = get_summarization_config()
    summarization_llm = create_summarization_llm()
    summarization_middleware = SummarizationMiddleware(
        model=summarization_llm,
        backend=store_backend,
        trigger=("tokens", sc["trigger_tokens"]),
        keep=("messages", sc["keep_messages"]),
    )

    # 权限控制：skills / memory / soul 为只读资源
    permissions = [
        FilesystemPermission(
            operations=["write"],
            paths=["/skills/**", "/memory/**", "/soul/**"],
            mode="deny",
        ),
    ]

    agent = create_deep_agent(
        model=llm,
        system_prompt="你是 FILA 品牌的金牌导购顾问。根据顾客信息和商品信息，撰写可以直接发送给顾客的营销话术。",
        skills=["/skills/"],
        middleware=[memory_middleware, summarization_middleware],
        permissions=permissions,
        backend=store_backend,
        store=store,
        checkpointer=checkpointer,
    )

    logger.info(
        "[agent] Agent 构建完成 (soul=%s, summarization_model=%s, trigger=%d tokens)",
        soul_name or "none", sc["model"], sc["trigger_tokens"],
    )
    return agent
