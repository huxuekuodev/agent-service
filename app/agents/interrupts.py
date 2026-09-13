"""中断（interrupt）与恢复（resume）协议：节点问什么、前端答什么。

为什么需要一个协议层：LangGraph 的 ``interrupt(value)`` 会把 ``value`` 冻结进 checkpoint，
恢复时 ``Command(resume=answer)`` 把 ``answer`` 原样交回同一个 ``interrupt()`` 调用点。
两端的结构一旦不统一，就会出现"用户点了同意但没有任何效果"这种静默错误（实测：
挂起状态下用普通消息重发**不会**消费挂起，只会带着新输入重跑并生成新的 interrupt）。

因此统一成两层结构，与 ask/answers 风格对齐，**天然支持一次中断问多个问题**：

节点侧问（:func:`build_ask`）::

    {
      "type": "plan_review",                     # 卡片类型（前端据此渲染）
      "title": "计划已生成，确认后开始执行",
      "items": [ {"plan_id": "task1", "name": "...", "desc": "..."} ],   # 给用户看的内容
      "questions": [                             # 可空：纯展示 + 允许留言
        {"id": "plan_review", "question": "需要调整吗？留空即按此计划执行",
         "options": [], "allow_custom": True, "custom_placeholder": "例如：把北京换成上海"}
      ]
    }

前端答（:class:`AskAnswer`，与 ``ask_user_question`` 的返回结构一致）::

    {"interrupt_id": "...", "answers": [ {"id": "plan_review", "selected": [], "custom": "把北京换成上海"} ]}

节点侧读（:func:`parse_answer`）::

    AskAnswer(answered=True, feedback="把北京换成上海", selected=[], continue_=False)

语义约定（节点自定义，协议只负责搬运）：
  - 没有 ``custom`` 且 ``selected`` 为空 → 用户"照此执行"（``continue_=True``）；
  - 有反馈文本 → 用户提了意见（``continue_=False`` + ``feedback``），节点据此回到规划节点重排。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "INTERRUPT_EVENT_TYPE",
    "AskAnswer",
    "build_ask",
    "extract_interrupt",
    "parse_answer",
    "question_of",
]

#: SSE 事件类型（前端契约，与 app/agents/events.EventType.INTERRUPT 一致）
INTERRUPT_EVENT_TYPE = "interrupt"

#: 默认问题 id（只有一个问题时的约定 id）
DEFAULT_QUESTION_ID = "review"


@dataclass
class AskAnswer:
    """解析后的用户答复（节点侧使用）。"""

    answered: bool = False
    """是否真的收到过答复（False = 首次执行到这里，属于"还没问"）。"""
    interrupt_id: str = ""
    continue_: bool = True
    """用户是否同意按原样继续（无反馈且无选择即视为同意）。"""
    feedback: str = ""
    """用户的自由意见（会作为 HumanMessage 回到规划节点）。"""
    selected: list[str] = field(default_factory=list)
    """用户在选项里选了什么（当前前端只用"展示 + 留言"，留作多问题扩展）。"""
    raw: dict[str, Any] = field(default_factory=dict)
    """原始载荷，便于排障与后续扩展。"""


def build_ask(
    *,
    kind: str,
    title: str = "",
    items: list[dict[str, Any]] | None = None,
    questions: list[dict[str, Any]] | None = None,
    content: str = "",
) -> dict[str, Any]:
    """构造 interrupt 载荷（节点侧唯一出口，避免各节点自己拼 dict）。

    Args:
        kind: 卡片类型（``plan_review`` / ``cleanup_confirm`` …），前端按它选渲染方式。
        title: 卡片标题。
        items: 展示给用户的条目（如计划步骤），**不含系统内部任务**。
        questions: 需要用户回答的问题（可空 = 纯展示 + 允许留言）。
        content: 附加说明文本。
    """
    return {
        "type": kind,
        "title": title,
        "content": content,
        "items": list(items or []),
        "questions": list(questions or []),
    }


def question_of(payload: dict[str, Any]) -> dict[str, Any] | None:
    """取第一个问题（单问题场景的便捷入口）。"""
    questions = payload.get("questions") or []
    return questions[0] if questions else None


def parse_answer(value: Any, *, interrupt_id: str = "") -> AskAnswer:
    """把 ``interrupt()`` 的返回值解析成 :class:`AskAnswer`。

    兼容三种历史/新增形态：
      1. 新协议：``{"answers": [{"id": ..., "selected": [...], "custom": "..."}]}``；
      2. 旧协议：``{"continue": true/false, "suggestion": "..."}``；
      3. 上面任意一种的 JSON 字符串（旧版本节点用 ``json.dumps`` 传参留下的形态）。
    """
    data: Any = value
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            # 纯文本答复：当作自由意见
            text = data.strip()
            return AskAnswer(answered=bool(text), interrupt_id=interrupt_id, continue_=not text, feedback=text, raw={"text": data})

    if not isinstance(data, dict):
        return AskAnswer(answered=False, interrupt_id=interrupt_id)

    answer = AskAnswer(answered=True, interrupt_id=str(data.get("interrupt_id") or interrupt_id), raw=data)

    # 形态 2：旧协议
    if "continue" in data and "answers" not in data:
        answer.continue_ = bool(data.get("continue"))
        answer.feedback = str(data.get("suggestion") or "").strip()
        return answer

    # 形态 1：新协议（可含多个问题的答案）
    selected: list[str] = []
    customs: list[str] = []
    for item in data.get("answers") or []:
        if not isinstance(item, dict):
            continue
        selected += [str(s) for s in (item.get("selected") or [])]
        custom = str(item.get("custom") or "").strip()
        if custom:
            customs.append(custom)

    answer.selected = selected
    answer.feedback = "\n".join(customs)
    answer.continue_ = not answer.feedback
    return answer


def extract_interrupt(chunk: Any) -> tuple[str, dict[str, Any]] | None:
    """从 stream chunk（``updates`` 通道）里提取中断信息。

    Returns:
        ``(interrupt_id, payload)``；没有中断时返回 None。``payload`` 若节点传的是字符串
        （旧形态），这里会尽量解析成 dict；解析不出来则包成 ``{"text": ...}``。
    """
    if not isinstance(chunk, dict):
        return None
    if chunk.get("type") != "updates":
        return None
    data = chunk.get("data")
    if not isinstance(data, dict):
        return None
    interrupts = data.get("__interrupt__")
    if not interrupts:
        return None

    first = interrupts[0] if isinstance(interrupts, (list, tuple)) and interrupts else None
    if first is None:
        return None
    interrupt_id = str(getattr(first, "id", "") or "")
    value = getattr(first, "value", first)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = {"text": value}
    if not isinstance(value, dict):
        value = {"text": str(value)}
    return interrupt_id, value
