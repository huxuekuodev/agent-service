"""
Step Dispatch Node + Fan-out Router：筛选可执行任务，标记 in_progress，并行派发。

职责分离：
  - step_dispatch_node（节点）：
      从 plan_tasks 筛选可执行任务（not_started + 依赖全部完成），
      **执行前请求用户确认计划**（interrupt），然后标记为 in_progress；
  - step_fan_out_router（纯路由函数）：
      读取更新后的 state，返回 [Send("general_agent", {...})] 并行派发，
      或返回 END（全部完成）。

人工确认（interrupt）设计：
  - 只在**确实有可执行任务**时中断（否则每次回到本节点都会多问一次，用户会莫名其妙）；
  - 确认卡片只展示**面向用户的任务**，系统内部任务（``skill_probe`` 等）不展示、也不需要批准；
  - 用户可以不选任何选项直接继续，也可以只留一句意见——有意见则回到规划节点重排计划
    （协议见 ``app/agents/interrupts.py``，与 ``approval.plan_review`` 开关联动）。
"""

from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.constants import Send
from langgraph.graph import END
from langgraph.types import Command, interrupt

from app.agents.interrupts import DEFAULT_QUESTION_ID, build_ask, parse_answer
from app.agents.subtask import SubTask
from app.agents.thread_state import ThreadState

#: 系统内部任务（校验/探测类）：不展示给用户、不需要用户批准
INTERNAL_TASK_IDS = frozenset({"skill_probe"})


def _inject_dep_results(task: SubTask, plan_tasks: list[SubTask]) -> str:
    """将 task.deps 中已完成依赖的 result 注入到 desc 中。

    替换 desc 中的 {plan_id} 占位符为对应依赖任务的 result 内容。
    """
    filled = task.desc
    for dep_id in task.deps or []:
        dep_task = next((t for t in plan_tasks if t.plan_id == dep_id), None)
        if dep_task and dep_task.result:
            placeholder = "{" + dep_id + "}"
            filled = filled.replace(placeholder, dep_task.result)
    return filled


async def step_dispatch_node(state: ThreadState, **kwargs) -> dict:
    """Step Dispatch Node：从 plan_tasks 筛选可执行任务并标记为 in_progress。

    可执行条件：
      - step_statuses == "not_started"
      - 所有 deps 已完成 (step_statuses == "completed")

    Returns:
        plan_tasks 状态更新（由 merge reducer 写回 ThreadState）。
    """
    plan_tasks = state.get("plan_tasks", [])
    if not plan_tasks:
        return {}

    status_map = {t.plan_id: t.step_statuses for t in plan_tasks}
    status_updates: list[SubTask] = []
    runnable: list[SubTask] = []

    for task in plan_tasks:
        if task.step_statuses != "not_started":
            continue

        # 检查依赖
        deps_ready = True
        for dep_id in task.deps or []:
            dep_status = status_map.get(dep_id)
            if dep_status != "completed":
                task.blocked_message = f"等待依赖任务 [{dep_id}] 完成"
                deps_ready = False
                break

        if deps_ready:
            runnable.append(task)

    # 执行前确认：只在本轮确实要派发任务时问一次；无意见即继续，有意见回规划节点重排
    approval_message = _request_plan_approval(runnable)
    if approval_message is not None:
        return Command(update={"messages": [approval_message]}, goto="plan_model_node")

    for task in runnable:
        task.step_statuses = "in_progress"
        task.blocked_message = ""
        status_updates.append(SubTask(plan_id=task.plan_id, step_statuses="in_progress"))

    if not status_updates:
        # 全部完成 → 返回空（不设置 completed），
        # 由 step_fan_out_router 路由回 plan_model_node 生成最终答案
        return {}

    return {"plan_tasks": status_updates}


def step_fan_out_router(state: ThreadState) -> list[Send] | str:
    """Fan-out 路由：读取 state，为每个 in_progress 任务创建 Send 派发。

    将已完成依赖的 result 注入到子任务 desc 中，使 general_agent 收到的描述完整。

    Returns:
        - list[Send]: 有可执行任务时并行派发到 execution_agent
        - "plan_model_node": 全部任务完成，回到规划节点审查并给出最终答案
        - END: 没有可派发任务且未全部完成（避免死循环）
    """
    plan_tasks = state.get("plan_tasks", [])
    if not plan_tasks:
        return END

    sends: list[Send] = []
    for task in plan_tasks:
        if task.step_statuses != "in_progress":
            continue

        # 将依赖结果注入 desc
        filled_desc = _inject_dep_results(task, plan_tasks)

        sends.append(
            Send(
                task.execution_agent,
                {
                    "plan_id": task.plan_id,
                    "task_name": task.name,
                    "task_desc": filled_desc,
                    "plan_tasks": plan_tasks,
                },
            )
        )

    if sends:
        return sends

    # 没有 in_progress 任务：
    #   - 存在 failed → 回规划节点审查（按技能错误规则生成恢复 DAG / 人工介入 / 直接答复）
    #   - 全部完成 → 回到规划节点审查并给出最终答案
    #   - 其他（阻塞/未完成）→ 结束（避免死循环）
    if any(t.step_statuses == "failed" for t in plan_tasks):
        return "plan_model_node"
    if all(t.step_statuses == "completed" for t in plan_tasks):
        return "plan_model_node"

    return END


def _visible_tasks(tasks: list[SubTask]) -> list[SubTask]:
    """面向用户的任务（过滤掉系统内部任务，如 skill_probe）。"""
    try:
        from app.config import get_app_config

        if get_app_config().approval.show_internal_tasks:
            return list(tasks)
    except Exception:
        pass
    return [t for t in tasks if t.plan_id not in INTERNAL_TASK_IDS]


def _request_plan_approval(runnable: list[SubTask]) -> BaseMessage | None:
    """执行前请求用户确认：返回"用户意见"消息（需回规划节点），或 None（继续执行）。

    - 开关关闭 / 本轮没有可执行任务 / 只有系统内部任务 → 不中断；
    - 用户未留言 → 继续执行；
    - 用户留言 → 返回 HumanMessage 交回规划节点（重新规划/修正计划）。
    """
    if not runnable:
        return None
    try:
        from app.config import get_app_config

        approval = get_app_config().approval
    except Exception:
        approval = None
    if approval is not None and not approval.plan_review:
        return None

    visible = _visible_tasks(runnable)
    if not visible:
        # 只有内部任务：无需用户确认，直接执行
        return None

    items = [{"plan_id": t.plan_id, "name": t.name, "desc": t.desc, "skill_id": t.skill_id} for t in visible]
    questions: list[dict] = []
    if approval is None or approval.allow_feedback:
        questions = [
            {
                "id": DEFAULT_QUESTION_ID,
                "question": "需要调整吗？可以直接继续执行，也可以写下你的意见（会按意见重新规划）。",
                "options": [],
                "allow_custom": True,
                "custom_placeholder": "例如：把北京换成上海",
            }
        ]

    payload = build_ask(
        kind="plan_review",
        title="计划已生成，确认后开始执行",
        items=items,
        questions=questions,
        content="以下是将要执行的步骤：",
    )
    answer = parse_answer(interrupt(payload))
    if not answer.feedback:
        return None

    lines = ["用户对计划提出了意见，请据此重新规划：", answer.feedback, "", "原计划：", _render_items(items)]
    return HumanMessage(content="\n".join(lines))


def _render_items(items: list[dict]) -> str:
    """把确认卡片里的步骤渲染成文本（回规划节点时的上下文）。"""
    return "\n".join(f"- {it.get('plan_id')}: {it.get('name')}｜{str(it.get('desc') or '')[:120]}" for it in items)


def parse_approved(approved: Any, task: str) -> BaseMessage | None:
    """兼容旧签名：解析 interrupt 返回值，返回"用户意见"消息或 None。

    新代码请用 :func:`_request_plan_approval`；此函数保留给既有测试/调用方。
    """
    answer = parse_answer(approved)
    if not answer.feedback:
        return None
    return HumanMessage(content=f"用户中断计划执行，给出建议：{answer.feedback}\n 之前的任务：\n{task}")
