"""Agent 服务层：封装 GraphAgent，管理会话与线程。

提供创建会话、按会话对话的能力，屏蔽 LangGraph 细节。
"""

from __future__ import annotations

import uuid
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.lead_agent.agent import GraphAgent
from app.config import get_app_config
from app.core.context import trace_id_ctx_var
from app.core.runtime import RunContext


class AgentService:
    """Agent 服务。

    管理会话（thread）与对话。每个会话对应一个 LangGraph thread（有独立的 checkpointer 状态）。
    """

    def __init__(self) -> None:
        self._app_config = get_app_config()
        self._checkpointer = InMemorySaver()
        self._run_context = RunContext(
            checkpointer=self._checkpointer,
            app_config=self._app_config,
        )
        # 会话 → GraphAgent 实例映射（每个会话独立 config）
        self._agents: dict[str, GraphAgent] = {}

    # ------------------------------------------------------------------
    # 会话管理
    # ------------------------------------------------------------------

    def create_session(self, *, model_name: str | None = None) -> dict:
        """创建一个新会话。

        Returns:
            {"session_id": "...", "thread_id": "..."}
        """
        session_id = uuid.uuid4().hex
        thread_id = session_id  # 会话 ID 即 thread ID

        config = self._build_config(thread_id=thread_id, model_name=model_name)
        agent = GraphAgent(config, self._run_context)
        self._agents[session_id] = agent

        return {"session_id": session_id, "thread_id": thread_id}

    def delete_session(self, session_id: str) -> bool:
        """删除会话。"""
        if session_id in self._agents:
            del self._agents[session_id]
            return True
        return False

    def get_session(self, session_id: str) -> GraphAgent | None:
        """获取会话对应的 agent。"""
        return self._agents.get(session_id)

    def list_sessions(self) -> list[str]:
        """列出所有活跃会话。"""
        return list(self._agents.keys())

    # ------------------------------------------------------------------
    # 对话
    # ------------------------------------------------------------------

    async def chat(self, session_id: str, message: str) -> list[dict]:
        """发送消息并等待完整回复。

        Returns:
            完整回复消息列表（含最终答案）。
        """
        agent = self._get_or_create_agent(session_id)
        state = {"messages": [HumanMessage(content=message)]}

        chunks = []
        async for st in agent.astream(state):
            chunks.append(st)
        return self._extract_messages(chunks)

    async def stream(self, session_id: str, message: str):
        """发送消息并流式返回事件。

        Yields:
            dict: {"type": "values"|"custom"|"messages", "data": ...}
        """
        agent = self._get_or_create_agent(session_id)
        state = {"messages": [HumanMessage(content=message)]}
        async for st in agent.astream(state):
            yield st

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _get_or_create_agent(self, session_id: str) -> GraphAgent:
        """获取会话对应 agent；不存在则用该 session_id 创建。"""
        agent = self._agents.get(session_id)
        if agent is not None:
            return agent
        # 用请求的 session_id 创建（而非新随机 id）
        thread_id = session_id
        config = self._build_config(thread_id=thread_id, model_name=None)
        agent = GraphAgent(config, self._run_context)
        self._agents[session_id] = agent
        return agent

    def _build_config(self, *, thread_id: str, model_name: str | None = None) -> RunnableConfig:
        trace_id = trace_id_ctx_var.get() or uuid.uuid4().hex
        configurable: dict[str, Any] = {
            "thread_id": thread_id,
            "trace_id": trace_id,
            "model_name": model_name or self._app_config.default_model.name,
        }
        return {"configurable": configurable}

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
