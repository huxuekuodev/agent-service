"""
主图 v2：plan_model_node → step_dispatch_node (fan-out via Send) → general_agent → 循环 → END。

step_fan_out_router 是路由函数（不是节点），由 add_conditional_edges 调用。
当返回 [Send(...)] 时 LangGraph 自动并行派发到 general_agent；
当返回 END 时流程结束。

设计说明（集群安全）：
  GraphAgent 是【无状态】的——编译后的图全局复用，不绑定 thread_id。
  每次 astream 调用通过 thread_id 参数动态构建 config，
  状态由共享 checkpointer（Postgres）按 thread_id 恢复。
  这样多节点负载均衡下，用户请求发散到任意节点都不会丢会话上下文。
"""

from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langfuse import Langfuse
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from app.agents.errors import should_retry
from app.agents.lead_agent import GraphContext, create_llm
from app.agents.nodes import (
    plan_model_node,
    step_dispatch_node,
    step_fan_out_router,
)
from app.agents.plan_storage import get_plan_storage
from app.agents.subagent.general_agent import general_agent
from app.agents.thread_state import ThreadState
from app.config import get_app_config
from app.core.runtime import RunContext

# 规划节点重试策略：仅对可恢复的 LLM 错误重试（超时/连接/5xx/429/服务繁忙），
# 欠费/认证失败等不可恢复错误不重试（直接返回友好提示）。
_PLAN_RETRY_POLICY = RetryPolicy(
    max_attempts=3,  # 首次 + 2 次重试
    retry_on=should_retry,  # 复用 v2 errors 模块的错误分类
    initial_interval=0.5,  # 首次重试前等待 0.5s
    backoff_factor=2.0,  # 指数退避：0.5 → 1 → 2s
    jitter=True,  # 加随机抖动防惊群
)


class GraphAgent:
    """无状态 Agent 图。

    编译一次、全局复用，不绑定任何会话。所有会话共享同一编译图，
    状态通过 checkpointer 按 thread_id 持久化/恢复。
    """

    def __init__(self, runcontext: RunContext):
        self._app_config = runcontext.app_config or get_app_config()
        self._checkpointer = runcontext.checkpointer
        self._agent: Any = None

    def _build_graph(self) -> Any:
        if self._agent is not None:
            return self._agent
        builder = StateGraph(ThreadState, context_schema=GraphContext)

        builder.add_node("plan_model_node", cast(Any, plan_model_node), retry_policy=_PLAN_RETRY_POLICY)
        builder.add_node("step_dispatch_node", cast(Any, step_dispatch_node))
        builder.add_node("general_agent", cast(Any, general_agent))

        builder.add_edge(START, "plan_model_node")

        # 规划 → 已完成（最终答案）直接结束；有子任务走调度；无任务（澄清/直接回复）也结束
        builder.add_conditional_edges(
            "plan_model_node",
            lambda s: END if s.get("completed") else ("step_dispatch_node" if s.get("plan_tasks") else END),
        )

        # 调度 → fan-out 路由：返回 [Send(...)] 并行派发，全部完成返回 "plan_model_node" 审查
        builder.add_conditional_edges(
            "step_dispatch_node",
            step_fan_out_router,
        )

        # general_agent 完成 → 回到调度继续下一轮
        builder.add_edge("general_agent", "step_dispatch_node")

        if self._checkpointer is not None:
            self._agent = builder.compile(checkpointer=self._checkpointer)
        else:
            self._agent = builder.compile()

        return self._agent

    async def astream(
        self,
        messages,
        *,
        thread_id: str,
        trace_id: str | None = None,
        model_name: str | None = None,
    ):
        """流式运行 agent。

        Args:
            messages: 输入消息（dict 或 list 或单条）。
            thread_id: 会话线程 ID（按请求传入，决定 checkpointer 恢复哪份状态）。
            trace_id: 追踪 ID。
            model_name: 可选，覆盖默认模型。
        """
        agent = self._build_graph()
        ctx = self.get_context(model_name=model_name)

        input_data: dict = {}
        if isinstance(messages, dict):
            input_data = messages
        elif isinstance(messages, list):
            input_data = {"messages": messages}
        else:
            input_data = {"messages": [messages]}

        for key, default in [
            ("plan_tasks", []),
            ("completed", False),
            ("user_message", ""),
            ("final_answer", ""),
        ]:
            input_data.setdefault(key, default)

        # 每次请求动态构建 config（thread_id 按请求传入）
        configurable: dict[str, Any] = {
            "thread_id": thread_id,
            "model_name": model_name or self._app_config.default_model.name,
        }
        if trace_id:
            configurable["trace_id"] = trace_id
        config: RunnableConfig = {"configurable": configurable}

        async for st in agent.astream(
            stream_mode=["values", "messages", "custom"],
            input=input_data,
            config=config,
            context=ctx,
            version="v2",
            subgraphs=True,
        ):
            yield st

    def get_context(self, model_name: str | None = None) -> GraphContext:
        config: RunnableConfig = {"configurable": {"model_name": model_name or self._app_config.default_model.name}}
        return GraphContext(
            app_config=self._app_config,
            plan_llm=create_llm(config),
            langfuse_client=Langfuse(),
            plan_storage=get_plan_storage(),
        )
