"""
V2 状态定义，支持澄清 + 策划 + 执行 + 审查循环。

plan_tasks 使用 merge_plan_tasks reducer：
  - 每个节点返回的 plan_tasks 会与现有列表 merge
  - 相同 plan_id：状态字段（step_statuses/result/blocked_message）总是更新
  - 相同 plan_id：**定义字段**（name/desc/deps/execution_agent/sort/skill_id）只在任务
    **尚未开始**（step_statuses == "not_started"）时可改——用户提出意见后重排计划就是要改它；
    已经 in_progress/completed/failed 的任务定义冻结（其结果属于旧定义，不能被悄悄改写）
  - 新的 subtask 会被追加
  - 新计划（create 场景）由 plan_model_node 用 Overwrite 整体替换，绕过此 reducer
"""

from copy import deepcopy
from typing import Annotated

from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages
from typing_extensions import TypedDict

from app.agents.subtask import SubTask

# 状态字段：总是允许覆盖（执行进度由执行节点回写）
_MUTABLE_FIELDS = {"step_statuses", "result", "blocked_message"}

# 定义字段：仅当任务尚未开始时允许覆盖（重排/修正计划），一旦开始即冻结
_DEFINITION_FIELDS = {"name", "desc", "deps", "execution_agent", "sort", "skill_id"}

#: 视为"尚未开始"的状态（此时任务定义可改）
_NOT_STARTED = "not_started"

#: 视为"没写"的值：默认值/空值不覆盖已有内容（PATCH 语义）
#: 否则一次只带 step_statuses 的状态回写就会把 result 清空、把已完成任务重置成 not_started。
_NEUTRAL_VALUES: dict[str, tuple[object, ...]] = {
    "step_statuses": (_NOT_STARTED, ""),
    "result": ("",),
    "blocked_message": ("",),
    "name": ("",),
    "desc": ("",),
    "skill_id": ("",),
}


def _is_provided(field: str, value: object) -> bool:
    """该字段本次是否"真的写了值"（None / 默认值 / 空串都算没写）。"""
    if value is None:
        return False
    if field == "deps":
        return bool(value)
    neutral = _NEUTRAL_VALUES.get(field)
    if neutral is not None:
        return value not in neutral
    return True


def merge_plan_tasks(current: list[SubTask], update: list[SubTask]) -> list[SubTask]:
    """Merge plan_tasks：状态字段总是更新，定义字段仅在任务未开始时更新。

    相同 plan_id：
      - step_statuses / result / blocked_message 总是覆盖（执行进度回写）；
      - name / desc / deps / execution_agent / sort / skill_id 只在原任务仍是
        ``not_started`` 时覆盖（用户提意见后重排计划必须能改这些字段）；
        已开始/已完成的任务定义冻结，避免结果与定义脱节。
    新 plan_id 的任务直接追加。
    """
    if not current:
        return [deepcopy(t) for t in (update or [])]
    if not update:
        return current

    existing = {t.plan_id: t for t in current}
    changed = False

    for t in update:
        if t.plan_id in existing:
            # 已有任务：状态字段总是更新；未开始的任务连定义字段一起更新
            orig = existing[t.plan_id]
            fields = set(_MUTABLE_FIELDS)
            if orig.step_statuses == _NOT_STARTED:
                fields |= _DEFINITION_FIELDS
            for field in fields:
                new_val = getattr(t, field, None)
                if not _is_provided(field, new_val):
                    continue
                if new_val != getattr(orig, field, None):
                    setattr(orig, field, deepcopy(new_val) if isinstance(new_val, (dict, list)) else new_val)
                    changed = True
        else:
            # 新任务：整体追加
            existing[t.plan_id] = deepcopy(t)
            changed = True

    if changed:
        return list(existing.values())
    return current


class ThreadState(TypedDict, total=False):
    # LangGraph 消息列表
    messages: Annotated[list[BaseMessage], add_messages]

    # 子任务列表（Plan agent 输出），使用自定义 merge reducer
    plan_tasks: Annotated[list[SubTask], merge_plan_tasks]

    # 完成标记
    completed: bool

    # 用户原始消息
    user_message: str

    # 最终答案
    final_answer: str

    # 由 step_fan_out_router 通过 Send 派发到执行 agent 时注入的字段
    plan_id: str
    task_name: str
    task_desc: str
