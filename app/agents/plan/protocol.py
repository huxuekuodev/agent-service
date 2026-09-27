"""计划提交协议：``submit_plan`` 工具 + 「提交即终态」中间件。

为什么是工具而不是 ``response_format``：

  LangChain 的结构化输出（``ToolStrategy``）靠 ``tool_choice=required`` 强制模型调用
  结构化输出工具；thinking 模型的思考模式只接受 ``auto``，强制值直接被 API 拒绝
  （``Thinking mode does not support this tool_choice``）。把契约做成普通工具后，请求
  形态在所有渠道一致，计划依旧是**被 schema 校验的结构化对象**，只是"什么时候提交"
  由模型决定、"提交得对不对"由 pydantic 判定。

两条路径都收敛到同一个 ``PlanOutput``：

  1. 参数合法 → 中间件写入应答 ToolMessage 并直接结束本轮（不再多花一次模型调用）；
  2. 参数不合法 → 交给 langgraph 的 ToolNode：抛出的校验错误会作为 ToolMessage 回到模型，
     模型据此修正后重新提交（这就是框架内置的"结构化输出失败重试"，只是现在归我们掌控）。
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import ValidationError

from app.agents.plan.schema import PlanOutput, PlanTask

__all__ = ["SUBMIT_ACK", "SUBMIT_TOOL_NAME", "PlanSubmissionMiddleware", "parse_submission", "submit_plan"]

#: 工具名（模型看到的名字，也是事件层需要忽略的内部调用）
SUBMIT_TOOL_NAME = "submit_plan"

#: 提交成功后回给模型的应答（内容会进入 transcript，写得可读一些便于排障/评估）
SUBMIT_ACK = "计划已提交并锁定；本轮结束，无需再输出任何内容。"


@tool(SUBMIT_TOOL_NAME, args_schema=PlanOutput)
async def submit_plan(action: str, title: str = "", tasks: list[PlanTask] | None = None, answer: str = "") -> str:
    """提交本轮计划（DAG）。

    规划完成、需要调整计划、或反思通过可直接回答时，**必须通过本工具提交**，且每次只提交一次。
    不要在正文里输出 JSON：计划的唯一入口就是这个工具。
    """
    return SUBMIT_ACK


def parse_submission(args: Any) -> PlanOutput | None:
    """把工具调用参数解析成 ``PlanOutput``（非法返回 None，交由工具节点报错给模型）。"""
    if not isinstance(args, dict):
        return None
    try:
        return PlanOutput.model_validate(args)
    except ValidationError:
        return None


class PlanSubmissionMiddleware(AgentMiddleware):
    """``submit_plan`` 是终态动作：参数合法时立即结束本轮 agent。

    不做这一步的话，模型在提交之后还会再补一句"计划已提交"之类的正文：既白付一次
    模型调用（thinking 模型尤其贵），也让 transcript 多出无意义消息。语义与框架内置
    结构化输出保持一致：写入应答 ToolMessage 后跳转到 END。
    """

    @hook_config(can_jump_to=["end"])
    async def aafter_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        """模型输出里只有一次合法的 ``submit_plan`` 调用时，应答并收尾。"""
        messages = state.get("messages") or []
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage):
            return None
        calls = list(getattr(last, "tool_calls", None) or [])
        if not calls or any(call.get("name") != SUBMIT_TOOL_NAME for call in calls):
            # 没有提交、或与其它工具混用（含重复提交）→ 交给 ToolNode 正常处理
            return None
        if any(parse_submission(call.get("args")) is None for call in calls):
            # 参数不合法 → 让 ToolNode 把校验错误回给模型，由模型修正后重新提交
            return None
        acks = [ToolMessage(content=SUBMIT_ACK, tool_call_id=call.get("id") or "", name=SUBMIT_TOOL_NAME) for call in calls]
        return {"messages": acks, "jump_to": "end"}
