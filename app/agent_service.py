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

# LangGraph 多流模式 + subgraphs 的流协议常量
_StreamModes = ("values", "messages", "custom", "updates", "debug")


def _normalize_stream_event(chunk: Any) -> dict | None:
    """将 LangGraph ``astream`` 的原始输出归一化为统一事件。

    LangGraph 1.2.10（v2 协议，``stream_mode=[...]`` + ``subgraphs=True``）
    的产出是 **StreamPart dict**（见 ``langgraph/types.py``）::

        {"type": "messages"|"values"|"custom", "ns": [...], "data": ..., "interrupts": ...}

    其中 ``data``:
      - ``messages``: ``(msg_chunk, metadata)`` 二元组
      - ``custom``: 传给 ``StreamWriter`` 的任意业务 dict（如 thinkMessage）
      - ``values``: 完整状态 dict

    同时兼容旧版元组形态 ``(mode, payload)`` / ``(namespace, (mode, payload))``。

    归一化结果::

        {"type": "values"|"messages"|"custom", "data": ...}

    无法识别时返回 ``None``（由调用方跳过）。
    """
    # 旧版子图元组形态: (namespace, (mode, payload)) — 取内层 (mode, payload)
    if isinstance(chunk, (tuple, list)) and len(chunk) == 2 and isinstance(chunk[0], (tuple, list)):
        inner = chunk[1]
        if isinstance(inner, (tuple, list)) and len(inner) == 2:
            chunk = inner
        else:
            return None

    # 新版 StreamPart dict: {type, ns, data, interrupts}
    if isinstance(chunk, dict) and chunk.get("type") in _StreamModes and "data" in chunk:
        return {"type": chunk["type"], "data": chunk.get("data")}

    # 旧版元组形态: (mode, payload)
    if isinstance(chunk, (tuple, list)) and len(chunk) == 2:
        mode, payload = chunk
        if mode in _StreamModes:
            return {"type": mode, "data": payload}

    # 兜底 dict 形态（单 stream_mode 或兼容情况）
    if isinstance(chunk, dict):
        return {"type": chunk.get("type", "values"), "data": chunk}

    return None


class AgentService:
    """Agent 服务。

    会话（session_id）即 LangGraph thread_id。所有会话共享一个无状态编译图，
    状态由共享 checkpointer 管理。
    """

    def __init__(self) -> None:
        self._app_config = get_app_config()
        self._checkpointer = create_checkpointer(self._app_config)
        self._checkpointer_ctx = None
        self._run_context: RunContext | None = None
        self._agent: GraphAgent | None = None

    async def __aenter__(self) -> AgentService:
        """进入生命周期：打开 postgres 连接池 + setup 建表（memory 直接可用）。"""
        # postgres 模式返回 Handle（需 async with 进入）；memory 直接返回 saver
        if hasattr(self._checkpointer, "__aenter__"):
            self._checkpointer_ctx = self._checkpointer
            saver = await self._checkpointer.__aenter__()
        else:
            saver = self._checkpointer

        self._run_context = RunContext(
            checkpointer=saver,
            app_config=self._app_config,
        )
        # 无状态图：全局单例，编译一次复用
        self._agent = GraphAgent(self._run_context)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        """退出生命周期：释放 postgres 连接池。"""
        if self._checkpointer_ctx is not None:
            await self._checkpointer_ctx.__aexit__(*exc)
            self._checkpointer_ctx = None
        self._agent = None

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

    def _require_agent(self) -> GraphAgent:
        """确保服务已进入生命周期（__aenter__ 初始化了 agent）。"""
        if self._agent is None:
            raise RuntimeError(
                "AgentService 未初始化：请使用 `async with AgentService() as svc:` "
                "进入生命周期后再调用对话接口。"
            )
        return self._agent

    async def chat(self, session_id: str, message: str) -> list[dict]:
        """发送消息并等待完整回复（返回消息字典列表）。"""
        self._require_agent()
        messages: list[dict] = []
        async for event in self.stream(session_id, message):
            if event["type"] == "values":
                data = event.get("data", {})
                msgs = data.get("messages", []) if isinstance(data, dict) else []
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
            elif event["type"] == "custom":
                messages.append({"type": "custom", "data": event.get("data")})
        return messages

    async def stream(self, session_id: str, message: str):
        """发送消息并流式返回事件。

        将 LangGraph 的原始流（``(mode, payload)`` / ``(namespace, (mode, payload))``
        元组，见 ``agent.astream(stream_mode=[...], subgraphs=True)``）归一化为统一事件::

            {"type": "values" | "messages" | "custom", "data": ...}

        ``messages`` 事件为增量 token，供前端流式渲染。
        """
        agent = self._require_agent()
        trace_id = trace_id_ctx_var.get() or uuid.uuid4().hex
        state = {"messages": [HumanMessage(content=message)]}
        async for st in agent.astream(
            state,
            thread_id=session_id,
            trace_id=trace_id,
        ):
            event = _normalize_stream_event(st)
            if event is not None:
                yield event

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _extract_messages(self, chunks: list) -> list[dict]:
        """从原始流 chunk 中提取最终消息列表。

        ``chunks`` 为 ``astream`` 原始输出（元组或 dict），见 :func:`_normalize_stream_event`。
        """
        messages: list[dict] = []
        for chunk in chunks:
            event = _normalize_stream_event(chunk)
            if event is None:
                continue
            ctype = event["type"]
            data = event.get("data", {})
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
