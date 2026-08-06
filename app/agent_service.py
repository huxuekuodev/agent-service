"""Agent 服务层：无状态图 + 共享 checkpointer，集群安全。

设计：
  - GraphAgent 是【无状态】编译图，全局单例，服务所有会话。
  - 状态通过共享 checkpointer（Postgres，集群部署）按 thread_id 持久化。
  - 用户请求发散到任意节点，都能从 checkpointer 恢复同一 thread 的上下文。
"""

from __future__ import annotations

import uuid
from typing import Any

from langchain_core.messages import HumanMessage

from app.agents.lead_agent.agent import GraphAgent
from app.config import get_app_config
from app.core.checkpointer import create_checkpointer
from app.core.context import trace_id_ctx_var
from app.core.runtime import RunContext


class AgentService:
    """Agent 服务。

    会话（session_id）即 LangGraph thread_id。所有会话共享一个无状态编译图，
    状态由共享 checkpointer 管理。
    """

    def __init__(self) -> None:
        self._app_config = get_app_config()
        self._checkpointer = create_checkpointer(self._app_config)
        self._run_context = RunContext(
            checkpointer=self._checkpointer,
            app_config=self._app_config,
        )
        # 无状态图：全局单例，编译一次复用
        self._agent = GraphAgent(self._run_context)

    # ------------------------------------------------------------------
    # 会话管理
    # ------------------------------------------------------------------

    def create_session(self, *, model_name: str | None = None) -> dict:
        """创建一个新会话。

        集群安全：不绑定任何节点。session_id 即 thread_id，
        后续任何节点的请求都能从共享 checkpointer 恢复。
        """
        session_id = uuid.uuid4().hex
        return {"session_id": session_id, "thread_id": session_id, "model_name": model_name}

    def delete_session(self, session_id: str) -> bool:
        """删除会话（从 checkpointer 删除状态）。

        注意：当前简化版不实现 checkpointer 删除，仅返回 True。
        生产环境应调用 checkpointer 的删除 API。
        """
        # TODO: 调用 checkpointer 删除该 thread 的状态
        return True

    def list_sessions(self) -> list[str]:
        """列出活跃会话。

        注意：简化版不维护会话注册表（集群无中心状态）。
        生产环境应从持久化存储查询会话列表。
        """
        return []

    # ------------------------------------------------------------------
    # 对话
    # ------------------------------------------------------------------

    async def chat(self, session_id: str, message: str) -> list[dict]:
        """发送消息并等待完整回复。"""
        trace_id = trace_id_ctx_var.get() or uuid.uuid4().hex
        state = {"messages": [HumanMessage(content=message)]}

        chunks = []
        async for st in self._agent.astream(
            state,
            thread_id=session_id,
            trace_id=trace_id,
        ):
            chunks.append(st)
        return self._extract_messages(chunks)

    async def stream(self, session_id: str, message: str):
        """发送消息并流式返回事件。

        Yields:
            dict: {"type": "values"|"custom"|"messages", "data": ...}
        """
        trace_id = trace_id_ctx_var.get() or uuid.uuid4().hex
        state = {"messages": [HumanMessage(content=message)]}
        async for st in self._agent.astream(
            state,
            thread_id=session_id,
            trace_id=trace_id,
        ):
            yield st

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _extract_messages(self, chunks: list[dict]) -> list[dict]:
        """从 astream 输出中提取最终消息列表。"""
        messages: list[dict] = []
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            ctype = chunk.get("type")
            data = chunk.get("data", chunk)
            if ctype == "values" and isinstance(data, dict):
                msgs = data.get("messages", [])
                for m in msgs:
                    content = getattr(m, "content", "")
                    if isinstance(content, str) and content.strip():
                        messages.append(
                            {
                                "type": type(m).__name__,
                                "content": content,
                                "name": getattr(m, "name", None),
                            }
                        )
            elif ctype == "custom":
                messages.append({"type": "custom", "data": data})
        return messages
