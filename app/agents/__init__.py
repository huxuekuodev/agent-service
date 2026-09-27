"""agent 编排域。

目录按「图节点 / 共享设施」组织：

    plan/        规划域：计划契约（schema）+ DAG 操作（dag）+ 提交协议（protocol）+ 提示词 + 节点
    dispatch/    派发节点：筛选就绪任务、计划确认中断、Send 并行派发
    execute/     执行节点（general_agent）：执行单个子任务并写回结果
    graph/       主图装配（GraphAgent）与运行时上下文（GraphContext）
    state/       图状态（ThreadState）与运行期任务模型（SubTask）
    common/      跨节点共享：事件输出层 / LLM 错误分类 / 中断协议 / 上下文注入辅助
    middlewares/ agent 中间件（澄清展示、悬空 tool_call 修复）
    skills/      SKILL 技能库与沙箱执行
    tools/       工具注册表与第三方工具
    evaluators/  节点内评估器实现（config `evaluators` 的 use 路径指向这里）
"""
