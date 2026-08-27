"""自定义记忆中间件：每次执行都重新加载记忆内容。

原版 ``deepagents.middleware.memory.MemoryMiddleware`` 的 ``before_agent`` 中有跳过逻辑::

    if "memory_contents" in state:
        return None

导致同一个 ``thread_id`` 重复执行时不会重新加载 AGENTS.md / SOUL.md。
本模块去掉了这个跳过逻辑，确保每次 invoke 都从 Store 重新读取最新记忆。

参考 ``50-DeepAgent/my_middleware/my_memory_middleware.py``。
"""

from __future__ import annotations

import re
from typing import Annotated, NotRequired, TypedDict

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    PrivateStateAttr,
)
from langchain_core.runnables import RunnableConfig
from deepagents.middleware._utils import append_to_system_message
from deepagents.middleware.memory import MEMORY_SYSTEM_PROMPT


class MemoryState(AgentState):
    """记忆状态，包含 memory_contents 私有字段。"""
    memory_contents: NotRequired[Annotated[dict[str, str], PrivateStateAttr]]


class MemoryStateUpdate(TypedDict):
    memory_contents: dict[str, str]


_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _strip_html_comments(text: str) -> str:
    return _HTML_COMMENT_RE.sub("", text)


class AlwaysLoadMemoryMiddleware(AgentMiddleware):
    """每次执行都重新加载记忆内容（不跳过）。

    与原版 ``MemoryMiddleware`` 的区别：
    - 去掉了 ``if "memory_contents" in state: return None`` 的跳过逻辑
    - 确保每次 invoke 都从 Store 重新读取最新的记忆内容
    - 同一个 thread_id 多次执行也能获取到最新的 AGENTS.md
    """

    state_schema = MemoryState

    def __init__(
        self,
        *,
        backend,
        sources: list[str],
        system_prompt: str | None = MEMORY_SYSTEM_PROMPT,
    ):
        self._backend = backend
        self.sources = sources
        self.system_prompt = system_prompt

    def _format_agent_memory(self, contents: dict[str, str]) -> str:
        """将记忆内容格式化为 ``<agent_memory>`` 标签包裹的文本。"""
        if not contents or self.system_prompt is None:
            return (
                self.system_prompt.format(agent_memory="(No memory loaded)")
                if self.system_prompt
                else ""
            )

        sections = []
        for path in self.sources:
            raw = contents.get(path)
            if not raw:
                continue
            stripped = _strip_html_comments(raw).rstrip()
            if stripped:
                sections.append(f"{path}\n\n{stripped}")

        if not sections:
            return self.system_prompt.format(agent_memory="(No memory loaded)")

        memory_body = "\n\n".join(sections)
        return self.system_prompt.format(agent_memory=memory_body)

    def before_agent(self, state, runtime, config: RunnableConfig) -> MemoryStateUpdate | None:
        """每次执行前都从 Store 重新加载记忆（不跳过）。"""
        contents: dict[str, str] = {}
        results = self._backend.download_files(list(self.sources))
        for path, response in zip(self.sources, results, strict=True):
            if response.error is not None:
                if response.error == "file_not_found":
                    continue
                raise ValueError(f"Failed to download {path}: {response.error}")
            if response.content is not None:
                contents[path] = response.content.decode("utf-8")
        return MemoryStateUpdate(memory_contents=contents)

    async def abefore_agent(self, state, runtime, config: RunnableConfig) -> MemoryStateUpdate | None:
        """异步版本：每次执行前都从 Store 重新加载记忆（不跳过）。"""
        contents: dict[str, str] = {}
        results = await self._backend.adownload_files(list(self.sources))
        for path, response in zip(self.sources, results, strict=True):
            if response.error is not None:
                if response.error == "file_not_found":
                    continue
                raise ValueError(f"Failed to download {path}: {response.error}")
            if response.content is not None:
                contents[path] = response.content.decode("utf-8")
        return MemoryStateUpdate(memory_contents=contents)

    def modify_request(self, request):
        """将记忆内容注入到系统提示词中。"""
        if self.system_prompt is None:
            return request
        contents = request.state.get("memory_contents", {})
        agent_memory = self._format_agent_memory(contents)
        new_system_message = append_to_system_message(
            request.system_message, agent_memory,
        )
        if new_system_message is request.system_message:
            return request
        return request.override(system_message=new_system_message)

    def wrap_model_call(self, request, handler):
        """同步：注入记忆后调用下一个中间件。"""
        return handler(self.modify_request(request))

    async def awrap_model_call(self, request, handler):
        """异步：注入记忆后调用下一个中间件。"""
        return await handler(self.modify_request(request))
