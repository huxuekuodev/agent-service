"""DAG 运行期操作：契约 → 运行期任务、拓扑校验、技能探测注入、就绪筛选。

规划节点与派发节点**共用**这里的 DAG 语义，避免"规划怎么写、派发怎么读"两套规则：

    PlanOutput（模型契约，schema.py）
        └─ to_subtask()  ──►  SubTask（运行期 DAG 节点，app/agents/state/subtask.py）
                                 ├─ validate_topology() / repair_topology()  ← 依赖必须存在
                                 ├─ inject_skill_probe()                     ← 技能前置校验任务
                                 ├─ pick_ready()                             ← 派发：就绪任务
                                 └─ render_plan_status()                     ← 回灌给模型的进度
"""

from __future__ import annotations

from collections.abc import Collection

from app.agents.plan.schema import SKILL_PROBE_ID, PlanOutput, PlanTask
from app.agents.state.subtask import SubTask

__all__ = [
    "INTERNAL_TASK_IDS",
    "SKILL_PROBE_ID",
    "inject_skill_probe",
    "pick_ready",
    "render_plan_status",
    "repair_topology",
    "skill_ids_of",
    "to_subtask",
    "validate_topology",
]

#: 系统内部任务（校验/探测类）：不展示给用户、不需要用户批准
INTERNAL_TASK_IDS = frozenset({SKILL_PROBE_ID})


def to_subtask(task: PlanTask) -> SubTask:
    """把模型给出的计划任务转成运行期任务。"""
    return SubTask(
        plan_id=task.plan_id,
        name=task.name,
        desc=task.desc,
        execution_agent=task.execution_agent,
        sort=task.sort,
        deps=list(task.deps or []),
        skill_id=task.skill_id,
    )


def skill_ids_of(tasks: list[SubTask]) -> list[str]:
    """计划里出现过的技能 id（按任务顺序去重，主技能即第一个技能任务）。"""
    seen: list[str] = []
    for t in tasks:
        if t.skill_id and t.skill_id not in seen:
            seen.append(t.skill_id)
    return seen


def inject_skill_probe(tasks: list[SubTask]) -> list[SubTask]:
    """把「技能前置校验」作为 DAG 首任务注入，并让技能任务依赖它。

    技能由**任务上的 `skill_id`** 表达：只要计划里出现了技能任务，就注入一个前置校验任务
    （技能可用性 + 沙箱环境），让下游任务一开始就知道环境是否可用。

    Args:
        tasks: 模型产出的业务任务。

    Returns:
        注入 skill_probe 后的任务列表（原任务 deps 前插 skill_probe，列表原地更新）。
    """
    skill_ids = skill_ids_of(tasks)
    if not skill_ids:
        return tasks
    primary = skill_ids[0]
    min_sort = min((t.sort for t in tasks), default=0)
    probe = SubTask(
        plan_id=SKILL_PROBE_ID,
        name=f"前置校验技能「{primary}」与沙箱环境",
        desc=f"[skill_probe] {primary}；本计划涉及技能 {skill_ids}；系统校验技能可用性 + 沙箱环境并注入技能文件清单/错误规则，结果写回本任务",
        execution_agent="general_agent",
        sort=min_sort - 1,
        deps=[],
        skill_id=primary,
    )
    for t in tasks:
        if SKILL_PROBE_ID not in t.deps:
            t.deps = [SKILL_PROBE_ID, *t.deps]
    return [probe, *tasks]


def validate_topology(plan: PlanOutput, known_ids: Collection[str] = ()) -> list[str]:
    """校验依赖指向的任务是否存在（模型看不到旧计划 id 时最容易出错的地方）。

    ``known_ids`` 是**旧计划**里已有的 plan_id（``action='update'`` 时允许被依赖）；
    ``action='create'`` 会整体替换旧计划，因此不传（等价于只能依赖本次任务）。

    Returns:
        人类可读的错误列表；为空表示拓扑合法。
    """
    available = set(plan.known_ids()) | (set(known_ids) if plan.action == "update" else set())
    errors: list[str] = []
    for task in plan.tasks:
        missing = sorted({d for d in (task.deps or []) if d not in available})
        if missing:
            hint = "旧计划里没有这些任务" if plan.action != "update" else "本计划与旧计划里都没有这些任务"
            errors.append(f"任务 {task.plan_id} 的 deps 指向不存在的 plan_id {missing}（{hint}）")
    return errors


def repair_topology(tasks: list[SubTask], known_ids: Collection[str]) -> tuple[list[SubTask], list[str]]:
    """剔除指向不存在任务的依赖（否则该任务会被**永久**阻塞：依赖永远等不到 completed）。

    Returns:
        ``(修复后的任务列表, 修复说明)``。
    """
    known = set(known_ids) | {t.plan_id for t in tasks}
    notes: list[str] = []
    for task in tasks:
        missing = [d for d in (task.deps or []) if d not in known]
        if missing:
            task.deps = [d for d in task.deps if d in known]
            notes.append(f"任务 {task.plan_id} 的依赖 {missing} 不存在，已剔除（否则会永久阻塞）")
    return tasks, notes


def pick_ready(tasks: list[SubTask]) -> list[SubTask]:
    """筛选就绪任务：``not_started`` 且依赖全部 ``completed``。

    未就绪的任务会写上 ``blocked_message``（缺失的依赖因此能一眼看出来）。

    Returns:
        可执行任务列表（顺序与 tasks 一致）。
    """
    status = {t.plan_id: t.step_statuses for t in tasks}
    ready: list[SubTask] = []
    for task in tasks:
        if task.step_statuses != "not_started":
            continue
        pending = [d for d in (task.deps or []) if status.get(d) != "completed"]
        if pending:
            task.blocked_message = f"等待依赖任务 {pending} 完成"
            continue
        ready.append(task)
    return ready


def render_plan_status(tasks: list[SubTask]) -> str:
    """把当前计划渲染成 ``<PlanStatus>`` 文本（审查/调整计划时回灌给模型）。"""
    return "\n".join(f"- [{t.step_statuses}] plan_id: {t.plan_id}: 任务名称: {t.name}: 执行结果：【{t.result or '待执行'}】" for t in tasks)
