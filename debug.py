#!/usr/bin/env python
"""
Debug script for Deer Agent Service.
在 Trae/VS Code 中直接运行（F5），可设置断点调试。

Requirements:
    Run with `uv run` from the agent-service/ directory:

        cd agent-service && uv run python debug.py

Usage:
    1. 在 agent.py 或 plan_model_node.py 等文件设置断点
    2. 按 F5 或使用 "Run and Debug" 面板
    3. 观察控制台输出规划/执行/最终答案
"""

import asyncio
import uuid

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agents.lead_agent.agent import GraphAgent
from app.config import get_app_config
from app.core.context import trace_id_ctx_var
from app.core.log import logger
from app.core.runtime import RunContext


class StreamPrinter:
    """成熟的消息打印器：处理 values / messages / custom 三种 stream 模式。

    - values：完整 state 快照，按消息 id 去重，只打印新消息
    - messages：LLM 消息 token 块，按 (id, step) 聚合后打印完整内容
    - custom：thinkMessage 状态消息，美化输出
    """

    def __init__(self):
        self._seen_msg_ids: set[str] = set()
        self._message_buffers: dict[tuple[str, int], list[str]] = {}
        self._last_ai_content: str = ""

    @property
    def final_answer(self) -> str:
        """最后一次 AI 回复内容。"""
        return self._last_ai_content

    @staticmethod
    def _msg_type(msg) -> str:
        """返回消息的中文类型标签。"""
        if isinstance(msg, HumanMessage):
            return "👤 用户"
        if isinstance(msg, AIMessage):
            if getattr(msg, "tool_calls", None):
                return "🤖 Agent(调用工具)"
            return "🤖 Agent"
        if isinstance(msg, ToolMessage):
            return "🔧 工具结果"
        if isinstance(msg, SystemMessage):
            return "⚙️ 系统"
        return "💬 消息"

    def _format_message(self, msg) -> str | None:
        """格式化单条消息为可读日志，返回 None 表示无需打印。"""
        msg_id = getattr(msg, "id", None)
        if msg_id:
            if msg_id in self._seen_msg_ids:
                return None
            self._seen_msg_ids.add(msg_id)

        label = self._msg_type(msg)
        name = getattr(msg, "name", None)

        tool_calls = getattr(msg, "tool_calls", None) or []
        if tool_calls:
            calls = ", ".join(tc.get("name", "?") for tc in tool_calls)
            return f"{label} → 调用工具: {calls}"

        if isinstance(msg, ToolMessage):
            content = (msg.content or "")[:500]
            return f"{label} [{name}]:\n{content}"

        content = (getattr(msg, "content", None) or "").strip()
        if not content:
            return None
        if name:
            return f"{label} [{name}]:\n{content}"
        return f"{label}: {content}"

    def handle_chunk(self, chunk: dict) -> None:
        """处理单个 stream chunk（兼容 v1/v2 包装）。"""
        if not isinstance(chunk, dict):
            return
        chunk_type = chunk.get("type", "values")
        data = chunk.get("data", chunk)

        if chunk_type == "custom":
            self._handle_custom(data)
        elif chunk_type == "values":
            self._handle_values(data)
        elif chunk_type == "messages":
            self._handle_messages(data)

    def _handle_custom(self, data) -> None:
        """custom: {"type": "thinkMessage", "messages": "...", "trace_id": ...}"""
        if not isinstance(data, dict):
            return
        status_type = data.get("type", "")
        message = str(data.get("messages", ""))
        if status_type == "thinkMessage":
            logger.info(f"💡 {message}")
        else:
            logger.info(f"📦 [{status_type}] {message}")

    def _handle_values(self, data) -> None:
        """values: 完整 state 快照，按消息 id 去重后打印新消息。"""
        if not isinstance(data, dict):
            return
        messages = data.get("messages") or []
        for msg in messages:
            if isinstance(msg, AIMessage):
                content = (getattr(msg, "content", None) or "").strip()
                if content:
                    self._last_ai_content = content
            line = self._format_message(msg)
            if line:
                logger.info(line)

    def _handle_messages(self, data) -> None:
        """messages: (message_chunk, metadata)，按 (id, step) 聚合 token 块。"""
        if not (isinstance(data, (tuple, list)) and len(data) == 2):
            return
        msg_chunk, metadata = data
        if not isinstance(msg_chunk, AIMessage):
            return

        msg_id = getattr(msg_chunk, "id", None) or ""
        step = (metadata or {}).get("langgraph_step", 0)
        key = (msg_id, step)

        content = getattr(msg_chunk, "content", "") or ""
        self._message_buffers.setdefault(key, []).append(content)

        if getattr(msg_chunk, "response_metadata", {}).get("finish_reason") is not None:
            full = "".join(self._message_buffers.pop(key, [])).strip()
            if full:
                logger.info(f"🖋️ Agent 回复: {full}")


async def main():
    trace_id = uuid.uuid4().hex
    trace_id_ctx_var.set(trace_id)
    logger.info("debug test start")

    app_config = get_app_config()
    from langchain_core.messages import HumanMessage

    from app.core.checkpointer import create_checkpointer

    # 独立服务：从 config.yaml 的 database 段创建共享 checkpointer
    checkpointer = create_checkpointer(app_config)
    runcontext = RunContext(checkpointer=checkpointer, app_config=app_config)
    # 无状态图：全局复用，thread_id 每次传入
    agent = GraphAgent(runcontext)
    userquery = "查询河北今天天气最凉爽的城市"
    state = {"messages": [HumanMessage(content=userquery)]}
    thread_id = "debug-thread-001"

    # 使用成熟的消息打印器
    printer = StreamPrinter()
    async for chunk in agent.astream(state, thread_id=thread_id, trace_id=trace_id):
        printer.handle_chunk(chunk)

    # 提取最终答案
    final_ai = printer.final_answer
    logger.info("✅ 最终答案（{} 字符）: {}", len(final_ai), final_ai[:200])


if __name__ == "__main__":
    asyncio.run(main())
