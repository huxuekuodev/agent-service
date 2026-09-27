"""规划域：计划（DAG）的契约、提交协议、提示词与规划节点。

对外只需两个入口：

- :data:`plan_model_node`：图里的规划节点（澄清 / 规划 / 审查）；
- :class:`PlanOutput` / :data:`submit_plan`：计划契约与模型提交协议。

分层（都在本包内，业务契约不依赖任何渠道特性）::

    schema.py   计划 = 一张合法 DAG（结构与不变式）
    dag.py      契约 → 运行期任务、拓扑校验/修复、就绪筛选（与派发节点共用）
    protocol.py submit_plan 工具 + 「提交即终态」中间件
    prompt.py   系统提示词 + 能力/技能索引注入
    node.py     节点编排（输入组装 → 跑 agent → 落库发事件）
"""

from app.agents.plan.dag import INTERNAL_TASK_IDS, SKILL_PROBE_ID, inject_skill_probe, pick_ready, render_plan_status, validate_topology
from app.agents.plan.node import plan_model_node
from app.agents.plan.protocol import SUBMIT_TOOL_NAME, submit_plan
from app.agents.plan.schema import PlanOutput, PlanTask

__all__ = [
    "INTERNAL_TASK_IDS",
    "SKILL_PROBE_ID",
    "SUBMIT_TOOL_NAME",
    "PlanOutput",
    "PlanTask",
    "inject_skill_probe",
    "pick_ready",
    "plan_model_node",
    "render_plan_status",
    "submit_plan",
    "validate_topology",
]
