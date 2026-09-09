"""
SubTask 子任务数据模型。

由 Plan agent 创建，每轮 step_dispatch_node 执行一个或多个无依赖的子任务。
"""

from pydantic import BaseModel, Field


class SubTask(BaseModel):
    """单个子任务。"""

    plan_id: str = Field(default="", description="子任务唯一标识")
    name: str = Field(default="", description="子任务名称（简短）")
    desc: str = Field(default="", description="子任务详细描述")
    execution_agent: str = Field(default="general_agent", description="执行此任务的 agent")
    sort: int = Field(default=0, description="执行顺序序号")
    deps: list[str] = Field(default_factory=list, description="依赖的子任务 plan_id 列表")
    result: str = Field(default="", description="子任务执行结果")
    step_statuses: str = Field(default="not_started", description="当前状态")
    blocked_message: str = Field(default="", description="如果阻塞，阻塞原因")
    skill_id: str = Field(default="", description="该任务所属技能（按技能 SOP 拆解时填写）")
    sop_step: str = Field(default="", description="对应的技能 SOP 步骤，如 step-01")
    error_code: str = Field(default="", description="执行失败时的错误码（供技能 errors.yaml 查询恢复）")
