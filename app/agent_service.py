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
from langgraph.types import Overwrite

from app.agents.interrupts import INTERRUPT_EVENT_TYPE, extract_interrupt
from app.agents.lead_agent.agent import GraphAgent
from app.config import get_app_config
from app.core.checkpointer import create_checkpointer
from app.core.context import trace_id_ctx_var, voice_mode_ctx_var
from app.core.log import logger
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


def _translate_event(event: dict) -> dict | None:
    """把归一化事件翻译成对外事件；不对外暴露的事件返回 None。

    ``updates`` 通道里藏着 ``__interrupt__``（LangGraph 的挂起信号）与内部状态更新：
    中断要转成前端可见的 ``interrupt`` 事件，其余状态更新不外发（避免内部消息上屏）。
    """
    if event.get("type") != "updates":
        return event
    found = extract_interrupt(event)
    if found is None:
        return None
    interrupt_id, payload = found
    return {"type": INTERRUPT_EVENT_TYPE, "data": {"interrupt_id": interrupt_id, "payload": payload}}


#: 哨兵：区分「没传 resume」与「resume=None」（后者是合法的恢复值）
_NO_RESUME: Any = object()


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
    # 会话（图状态）
    # ------------------------------------------------------------------

    async def delete_thread(self, thread_id: str) -> bool:
        """删除该 thread 的 checkpoint（会话逻辑删除时调用）。

        会话元数据/消息在业务库（``app.session.store``）；这里只管 agent 运行态。
        失败不抛出：checkpoint 清理属于"尽力而为"，残留状态不影响用户可见历史。
        """
        if not thread_id:
            return False
        try:
            saver = self._run_context.checkpointer if self._run_context else None
            delete = getattr(saver, "adelete_thread", None)
            if delete is None:
                return False
            await delete(thread_id)
            logger.info("已清理 checkpoint: thread={}", thread_id)
            return True
        except Exception as exc:
            logger.warning("清理 checkpoint 失败: thread={} err={}", thread_id, exc)
            return False

    # ------------------------------------------------------------------
    # 对话
    # ------------------------------------------------------------------

    def _require_agent(self) -> GraphAgent:
        """确保服务已进入生命周期（__aenter__ 初始化了 agent）。"""
        if self._agent is None:
            raise RuntimeError("AgentService 未初始化：请使用 `async with AgentService() as svc:` 进入生命周期后再调用对话接口。")
        return self._agent

    async def chat(self, thread_id: str, message: str) -> list[dict]:
        """发送消息并等待完整回复（返回消息字典列表）。"""
        self._require_agent()
        messages: list[dict] = []
        async for event in self.stream(thread_id, message):
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

    async def stream(
        self,
        thread_id: str,
        message: str | None = None,
        *,
        resume: Any = _NO_RESUME,
        usage: Any = None,
        voice: bool = False,
    ):
        """发送消息（或恢复中断）并流式返回事件。

        将 LangGraph 的原始流归一化为统一事件::

            {"type": "custom" | "interrupt", "data": ...}

        - ``custom``：节点业务事件（前端渲染进度/答复）；
        - ``interrupt``：运行被 ``interrupt()`` 挂起，``data = {"interrupt_id", "payload"}``，
          前端据此渲染确认卡片，用户答复后用 ``resume=`` 再次调用本方法继续。

        Args:
            thread_id: 会话 ``checkpoint_thread_id``（默认等于 session_id）。
            message: 用户消息（新一轮对话；``resume`` 已传时可省略）。
            resume: 恢复挂起的运行（``Command(resume=<用户答复>)``）。
            usage: 可选用量采集器（``app.llm.usage.UsageCollector``）。
            voice: 是否语音通话模式（节点会改用口语化短句回答，便于朗读）。
        """
        agent = self._require_agent()
        trace_id = trace_id_ctx_var.get() or uuid.uuid4().hex
        token = voice_mode_ctx_var.set(bool(voice))

        try:
            async for event in self._stream_events(agent, thread_id, message, trace_id=trace_id, usage=usage, resume=resume):
                yield event
        finally:
            voice_mode_ctx_var.reset(token)

    async def _stream_events(self, agent: GraphAgent, thread_id: str, message: str | None, *, trace_id: str, usage: Any, resume: Any):
        """真正跑图并翻译事件（voice 模式只影响上下文，不影响这里）。"""
        if resume is _NO_RESUME:
            state: Any = {"messages": [HumanMessage(content=message or "")]}
            async for st in agent.astream(state, thread_id=thread_id, trace_id=trace_id, usage=usage):
                event = _normalize_stream_event(st)
                if event is None:
                    continue
                translated = _translate_event(event)
                if translated is not None:
                    yield translated
            return

        async for st in agent.astream(None, thread_id=thread_id, trace_id=trace_id, usage=usage, resume=resume):
            event = _normalize_stream_event(st)
            if event is None:
                continue
            translated = _translate_event(event)
            if translated is not None:
                yield translated

    async def prepare_turn(self, thread_id: str) -> dict[str, Any] | None:
        """新一轮对话开始前的一次状态检查（一次读取，做两件事）：

        1. 有挂起中断 → 返回它（调用方应拒绝 /chat，引导走 /resume）；
        2. 有卡在 ``in_progress`` 的任务 → **复位为 not_started**（上一轮被用户打断留下的），
           否则沿用旧计划时会看到一批"永远进行中"的任务，dispatch 既不重跑也无法判定完成。

        Returns:
            ``{"interrupt_id", "payload", "next"}`` 或 None。
        """
        try:
            graph = self._require_agent()._build_graph()
            config = {"configurable": {"thread_id": thread_id}}
            snapshot = await graph.aget_state(config)
        except Exception as exc:
            logger.warning("读取会话状态失败: thread={} err={}", thread_id, exc)
            return None

        interrupts = list(getattr(snapshot, "interrupts", None) or [])
        if interrupts:
            found = extract_interrupt({"type": "updates", "data": {"__interrupt__": interrupts}})
            if found is None:
                return None
            interrupt_id, payload = found
            return {"interrupt_id": interrupt_id, "payload": payload, "next": list(getattr(snapshot, "next", ()) or [])}

        # 没有挂起：清理上一轮被打断留下的 in_progress 任务（自愈）
        await self._reset_inflight(thread_id, getattr(snapshot, "values", None) or {})
        return None

    async def _reset_inflight(self, thread_id: str, values: dict[str, Any]) -> int:
        """把 ``in_progress`` 任务复位为 ``not_started``（合并 reducer 视 not_started 为"没写"，
        因此必须用 Overwrite 整体替换）。"""
        tasks = list(values.get("plan_tasks") or [])
        inflight = [t for t in tasks if t.step_statuses == "in_progress"]
        if not inflight:
            return 0
        fixed = [t.model_copy(update={"step_statuses": "not_started"}) if t.step_statuses == "in_progress" else t for t in tasks]
        try:
            graph = self._require_agent()._build_graph()
            await graph.aupdate_state({"configurable": {"thread_id": thread_id}}, {"plan_tasks": Overwrite(value=fixed)})
            logger.info("已复位上一轮被打断的任务: thread={} 数量={}", thread_id, len(inflight))
        except Exception as exc:
            logger.warning("复位被打断任务失败: thread={} err={}", thread_id, exc)
            return 0
        return len(inflight)

    async def reset_inflight_tasks(self, thread_id: str) -> int:
        """把卡在 ``in_progress`` 的任务复位为 ``not_started``（用户打断后清理）。

        为什么要它：图运行被用户中途打断时，任务可能已经标记 in_progress 却没跑完；
        下次请求若走 update（沿用旧计划）就会看到一批"永远进行中"的任务，
        dispatch 既不会重跑它们、也无法判定全部完成 → 会话假死。
        复位后下一轮可以正常重新派发。

        注意用 ``Overwrite`` 整体替换：合并 reducer 把 ``not_started`` 视为"没写"（防误重置），
        普通更新到不了这里。

        Returns:
            被复位的任务数。
        """
        try:
            graph = self._require_agent()._build_graph()
            config = {"configurable": {"thread_id": thread_id}}
            snapshot = await graph.aget_state(config)
            tasks = list((getattr(snapshot, "values", None) or {}).get("plan_tasks") or [])
            inflight = [t for t in tasks if t.step_statuses == "in_progress"]
            if not inflight:
                return 0
            fixed = [t.model_copy(update={"step_statuses": "not_started"}) if t.step_statuses == "in_progress" else t for t in tasks]
            await graph.aupdate_state(config, {"plan_tasks": Overwrite(value=fixed)})
            logger.info("已复位被打断的任务: thread={} 数量={}", thread_id, len(inflight))
            return len(inflight)
        except Exception as exc:
            logger.warning("复位被打断任务失败: thread={} err={}", thread_id, exc)
            return 0

    async def pending_interrupt(self, thread_id: str) -> dict[str, Any] | None:
        """查询该 thread 是否有挂起的中断（前端刷新后恢复确认卡片用）。

        Returns:
            ``{"interrupt_id": ..., "payload": {...}}``；无挂起返回 None。
        """
        try:
            graph = self._require_agent()._build_graph()
            snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        except Exception as exc:
            logger.warning("读取挂起中断失败: thread={} err={}", thread_id, exc)
            return None
        interrupts = list(getattr(snapshot, "interrupts", None) or [])
        if not interrupts:
            return None
        payload = extract_interrupt({"type": "updates", "data": {"__interrupt__": interrupts}})
        if payload is None:
            return None
        interrupt_id, value = payload
        return {"interrupt_id": interrupt_id, "payload": value, "next": list(getattr(snapshot, "next", ()) or [])}

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
