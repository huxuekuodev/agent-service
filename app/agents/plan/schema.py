"""计划（DAG）的业务契约。

这里是「计划」这一业务概念的**唯一定义处**：模型提交的计划必须满足的结构与不变式。
规划节点（``node.py``）用它产出计划，派发节点（``app/agents/dispatch``）用它执行计划，
DAG 拓扑校验与就绪判定在 ``dag.py``。

设计要点（为什么不用 LangChain 的 ``response_format``）：

  结构化输出在 provider 侧是靠「强制模型调用结构化工具」实现的（请求带
  ``tool_choice=required``），而 thinking 模型只接受 ``auto`` —— 于是"计划必须是
  DAG"这个**业务契约**被绑死在**渠道特性**上，换渠道就得在渠道层打补丁。

  把契约做成一个**普通工具**（``submit_plan``，见 ``protocol.py``）后：
    - 请求形态在所有渠道一致，渠道层零特例；
    - 参数由 pydantic 校验（本模块的不变式），不合法时工具报错、模型自我修正；
    - 业务语义（唯一 id、依赖存在、无环）由代码保证，而不是靠提示词自觉。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, Field, model_validator

__all__ = ["PlanTask", "PlanOutput", "PlanAction", "SKILL_PROBE_ID", "PLAN_ID_PATTERN"]

#: 技能上下文探测任务的固定 plan_id（系统注入，模型不得占用）
SKILL_PROBE_ID = "skill_probe"

#: plan_id 允许的字符（会作为 ``{plan_id}`` 占位符与 DAG 键使用）
PLAN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

PlanAction = Literal["create", "update", "complete"]


class PlanTask(BaseModel):
    """计划中的单个子任务（DAG 的一个节点）。"""

    plan_id: str = Field(description="子任务唯一标识，如 task1 / task2")
    name: str = Field(description="子任务名称（简短）")
    desc: str = Field(description="子任务详细描述。可用 {其他任务plan_id} 引用依赖任务的结果")
    execution_agent: str = Field(default="general_agent", description="执行此任务的 agent")
    sort: int = Field(default=0, description="执行顺序序号")
    deps: list[str] = Field(default_factory=list, description="依赖的子任务 plan_id 列表")
    skill_id: str = Field(default="", description="该任务执行的技能 id（一个技能=一个任务，整体执行，不拆步骤；可选）")

    @model_validator(mode="after")
    def _check_task(self) -> PlanTask:
        """节点级不变式：标识可用、内容非空、无自依赖。"""
        if not PLAN_ID_PATTERN.fullmatch(self.plan_id or ""):
            raise ValueError(f"plan_id 只能包含字母/数字/下划线/短横线，收到 {self.plan_id!r}")
        if self.plan_id == SKILL_PROBE_ID:
            raise ValueError(f"plan_id={SKILL_PROBE_ID!r} 是系统内部任务保留标识，不要创建它（系统会自动注入）")
        if not self.name.strip():
            raise ValueError(f"任务 {self.plan_id} 的 name 不能为空")
        if not self.desc.strip():
            raise ValueError(f"任务 {self.plan_id} 的 desc 不能为空")
        if self.plan_id in (self.deps or []):
            raise ValueError(f"任务 {self.plan_id} 不能依赖自己")
        return self


class PlanOutput(BaseModel):
    """规划节点的输出契约（= ``submit_plan`` 工具的参数）。"""

    action: PlanAction = Field(description="create: 创建全新计划（替换旧计划）；update: 更新现有计划状态；complete: 反思通过，直接给答案")
    title: str = Field(default="", description="计划标题")
    tasks: list[PlanTask] = Field(default_factory=list, description="子任务列表（技能任务在任务上标注 skill_id）")
    answer: str = Field(default="", description="action=complete 时的最终答案文本，其他情况为空字符串")

    @model_validator(mode="after")
    def _check_plan(self) -> PlanOutput:
        """计划级不变式：动作与任务自洽、id 唯一、DAG 无环。

        这些都能在不看历史状态的前提下判断，因此放在 schema 里 —— 由工具参数校验
        触发，非法计划会带着错误信息回到模型，让模型自己修正。
        """
        ids = [t.plan_id for t in self.tasks]
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        if duplicated:
            raise ValueError(f"plan_id 必须唯一，重复的有：{duplicated}")

        if self.action == "complete":
            if self.tasks:
                raise ValueError("action='complete' 表示反思通过、直接给答案，tasks 必须为空")
            if not self.answer.strip():
                raise ValueError("action='complete' 时 answer 必须填写最终答案")
        else:
            if not self.tasks:
                raise ValueError(f"action={self.action!r} 必须给出至少一个子任务")
            if self.answer.strip():
                raise ValueError(f"action={self.action!r} 时 answer 必须为空字符串")

        if self.action == "create":
            # create 会整体替换旧计划，因此依赖只能指向本次给出的任务
            unknown = sorted({d for t in self.tasks for d in t.deps if d not in set(ids)})
            if unknown:
                raise ValueError(f"action='create' 时 deps 只能指向本计划的 plan_id，未定义的有：{unknown}（怀疑要依赖旧计划请用 action='update'）")

        cycle = _find_cycle(self.tasks, set(ids))
        if cycle:
            raise ValueError(f"deps 存在循环依赖：{' -> '.join(cycle)}")

        return self

    def known_ids(self) -> set[str]:
        """本计划内出现的 plan_id 集合。"""
        return {t.plan_id for t in self.tasks}


def _find_cycle(tasks: Iterable[PlanTask], candidate_ids: set[str]) -> list[str]:
    """在「本次给出的任务」诱导出的子图上找环（忽略指向本计划外旧任务的依赖）。

    Returns:
        环上的 plan_id 序列（无环时为空列表）。
    """
    deps_of = {t.plan_id: [d for d in (t.deps or []) if d in candidate_ids] for t in tasks}
    state: dict[str, int] = {}  # 0=未访问 1=访问中 2=已完成
    stack: list[str] = []

    def visit(node: str) -> list[str]:
        if state.get(node) == 1:
            return [*stack[stack.index(node) :], node]
        if state.get(node) == 2:
            return []
        state[node] = 1
        stack.append(node)
        for dep in deps_of.get(node, []):
            found = visit(dep)
            if found:
                return found
        stack.pop()
        state[node] = 2
        return []

    for node in deps_of:
        found = visit(node)
        if found:
            return found
    return []
