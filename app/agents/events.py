"""节点 → 前端的事件输出层（统一出口，避免散落的 `writer({...})` 硬编码）。

设计：
  - 事件统一走 LangGraph 的 custom stream，schema 扁平：``{"type": <EventType>, "phase": <节点>, ...字段}``；
  - 节点只调用 :class:`Output` 的方法，不直接构造 dict、不直接碰 ``get_stream_writer``；
  - 无运行上下文（单测 / 脚本直调）时安全降级为 no-op，不影响主流程；
  - 事件类型集中在 :class:`EventType`，字段由方法签名约束（IDE 可提示、改一处即全局生效）。

事件一览（前端契约）::

    {"type": "thinkMessage", "messages": "📋 分析需求，制定执行计划..."}   # 阶段提示（与既有前端兼容）
    {"type": "thinking",     "delta": "..."}                              # 模型流式增量（打字机）
    {"type": "tool_call",    "name": "sandbox_run", "args": {...}}          # 工具调用
    {"type": "tool_result",  "name": "sandbox_run", "result": "...", "ok": true}
    {"type": "plan",         "action": "create", "title": "...", "tasks": [...]}
    {"type": "step",         "plan_id": "task1", "status": "started", "detail": "..."}
    {"type": "clarify",      "content": "问题", "clarification_type": "missing_info", "options": [...]}
    {"type": "answer",       "content": "最终答复", "final": true}
    {"type": "error",        "messages": "..."}
    {"type": "end"}
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from app.core.log import logger

__all__ = ["EventType", "StepStatus", "Output", "ToolCallAccumulator", "humanize_tool_call"]


#: 内部机制类型（结构化输出等）：不向前端发事件，避免 dump 上屏
_INTERNAL_TOOLS = frozenset({"PlanOutput", "ResponseFormat"})


class EventType(StrEnum):
    """前端事件类型（字符串值即 SSE payload 的 ``type``）。"""

    THINK = "thinkMessage"
    """阶段提示（保留原名以兼容既有前端）。"""
    THINKING = "thinking"
    """模型流式增量（打字机效果）。"""
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    PLAN = "plan"
    STEP = "step"
    CLARIFY = "clarify"
    ANSWER = "answer"
    ERROR = "error"
    END = "end"


class StepStatus(StrEnum):
    """任务（step）状态。"""

    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class Output:
    """节点事件输出器。

    Args:
        phase: 事件来源节点名（``plan_node`` / ``general_agent`` 等），便于前端分组与日志定位。
        trace_id: 关联的 trace（可选，非空时写入事件）。
    """

    def __init__(self, phase: str, *, trace_id: str = "") -> None:
        self._phase = phase
        self._trace_id = trace_id or ""

    # ------------------------------------------------------------------ 发射基础

    def _emit(self, event_type: EventType, **fields: Any) -> None:
        """写入 custom 流并记日志；无运行上下文时静默降级。"""
        payload: dict[str, Any] = {"type": str(event_type), "phase": self._phase}
        if self._trace_id:
            payload["trace_id"] = self._trace_id
        payload.update({k: v for k, v in fields.items() if v is not None})

        try:
            from langgraph.config import get_stream_writer

            writer = get_stream_writer()
            writer(payload)
        except Exception:
            # 单测 / 脚本直调场景没有 stream writer，降级为仅日志
            pass
        logger.debug("[{}][{}] {}", self._phase, event_type, _summarize(fields))

    # ------------------------------------------------------------------ 事件方法

    def think(self, message: str) -> None:
        """阶段提示（展示在思考条）。"""
        self._emit(EventType.THINK, messages=message)

    def thinking(self, delta: str) -> None:
        """模型流式增量（打字机）。"""
        if delta:
            self._emit(EventType.THINKING, delta=delta)

    def plan(self, *, action: str, title: str = "", tasks: list[dict[str, Any]] | None = None) -> None:
        """规划结果（任务清单）。"""
        items = tasks or []
        self._emit(EventType.PLAN, action=action, title=title, tasks=items, task_count=len(items))

    def step(
        self,
        *,
        plan_id: str,
        status: StepStatus | str,
        name: str = "",
        detail: str = "",
        skill_id: str = "",
    ) -> None:
        """任务执行状态（开始/完成/失败）。"""
        self._emit(EventType.STEP, plan_id=plan_id, status=str(status), name=name, detail=_summarize(detail, 300), skill_id=skill_id)

    def tool_call(self, *, name: str, args: Any = None, plan_id: str = "") -> None:
        """工具调用（执行过程可视化）；内部机制类型静默跳过。"""
        if name in _INTERNAL_TOOLS:
            return
        self._emit(EventType.TOOL_CALL, name=name, args=args if isinstance(args, (dict, list)) else _summarize(str(args or ""), 300), plan_id=plan_id)

    def tool_result(self, *, name: str, result: str = "", ok: bool = True, plan_id: str = "") -> None:
        """工具返回（截断展示）；内部机制类型（如结构化输出）静默跳过。"""
        if name in _INTERNAL_TOOLS:
            return
        self._emit(EventType.TOOL_RESULT, name=name, result=_summarize(result, 500), ok=ok, plan_id=plan_id)

    def clarify(self, *, content: str, clarification_type: str = "", options: list[str] | None = None, context: str = "") -> None:
        """澄清问题（前端可渲染为卡片）。"""
        self._emit(EventType.CLARIFY, content=content, clarification_type=clarification_type, options=options, context=context)

    def answer(self, *, content: str, final: bool = True) -> None:
        """最终答复。"""
        self._emit(EventType.ANSWER, content=content, final=final)

    def error(self, message: str, *, plan_id: str = "") -> None:
        """错误提示。"""
        self._emit(EventType.ERROR, messages=message, plan_id=plan_id)

    def end(self) -> None:
        """本轮结束。"""
        self._emit(EventType.END)


class ToolCallAccumulator:
    """累积流式 ``tool_call_chunks``，拼出完整的工具调用（name/args/id）。

    用于把"模型正在调用哪个工具、参数是什么"实时推给前端：
    流式分片先喂给 :meth:`feed`，返回本次可发射的完整调用列表。
    """

    def __init__(self, ignore_names: set[str] | None = None) -> None:
        self._slots: dict[int, dict[str, Any]] = {}
        self._ignore = ignore_names or set()

    def feed(self, chunk: Any) -> list[dict[str, Any]]:
        """喂入一个流式 chunk，返回本轮新完成的工具调用。"""
        for tc in getattr(chunk, "tool_call_chunks", None) or []:
            idx = tc.get("index", 0)
            slot = self._slots.setdefault(idx, {"name": "", "args": "", "id": None, "emitted": False})
            if tc.get("name"):
                slot["name"] += tc["name"]
            if tc.get("args"):
                slot["args"] += tc["args"]
            if tc.get("id"):
                slot["id"] = tc["id"]

        ready: list[dict[str, Any]] = []
        for idx, slot in self._slots.items():
            if slot["emitted"] or not slot["name"] or not slot["args"]:
                continue
            slot["emitted"] = True
            if slot["name"] in self._ignore:
                continue
            try:
                args = json.loads(slot["args"])
            except json.JSONDecodeError:
                args = {"_raw": _summarize(slot["args"], 200)}
            ready.append({"index": idx, "id": slot["id"], "name": slot["name"], "args": args})
        return ready


def humanize_tool_call(name: str, args: dict[str, Any] | None = None) -> str:
    """把工具调用翻译成一句人话（用于阶段提示）。"""
    args = args or {}
    match name:
        case "ask_clarification":
            return f"需要向你确认：{args.get('question', '')}"
        case "PlanOutput":
            return f"规划：{args.get('title', '')}"
        case _:
            return f"调用工具 {name}"


def _summarize(content: Any, limit: int = 200) -> str:
    """单行摘要（日志 / 事件展示用）。"""
    if content is None:
        return ""
    text = str(content).replace("\n", " ").strip()
    return text if len(text) <= limit else f"{text[:limit]}…"
