"""
规划节点（合并澄清 + 规划 + 审查）。

职责：
  1. 澄清：分析用户输入，模糊或缺失信息时调用 ask_clarification
  2. 规划：需求明确后拆解为 SubTask DAG（模型直接输出计划 JSON）
  3. 审查：执行后审查结果，决定完成或 replan

设计说明：
  - 不再使用 create_plan / update_plan 工具（绕了三层间接：工具→bridge→哨兵/reducer）
  - 模型通过结构化输出直接产出计划（PlanOutput），plan_model_node 解析为 SubTask
  - 新计划（用户新需求）→ 用 Overwrite 整体替换旧计划（绕过 merge reducer）
  - 状态更新（执行节点回写）→ 继续用 merge reducer 合并
  - 仅保留 ask_clarification 工具（经 get_plan_tools 注入）
"""

import asyncio
from typing import Any, cast

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langfuse import Langfuse
from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime
from langgraph.types import Overwrite
from pydantic import BaseModel, Field

from app.agents.current_time import has_current_time_for_today
from app.agents.errors import build_error_fallback_message, classify_llm_error
from app.agents.evaluation.plan_evaluator import EvaluationInput
from app.agents.evaluation.registry import create_evaluator
from app.agents.lead_agent import GraphContext, create_llm_with_name
from app.agents.middlewares.clarification_middleware import ClarificationMiddleware
from app.agents.middlewares.dangling_tool_call_middleware import DanglingToolCallMiddleware
from app.agents.nodes.constants import THINK_MES
from app.agents.subtask import SubTask
from app.agents.thread_state import ThreadState
from app.core.context import trace_id_ctx_var
from app.core.log import logger


class PlanTask(BaseModel):
    """计划中的单个子任务（模型结构化输出）。"""

    plan_id: str = Field(description="子任务唯一标识，如 task1 / task2")
    name: str = Field(description="子任务名称（简短）")
    desc: str = Field(description="子任务详细描述。可用 {其他任务plan_id} 引用依赖任务的结果")
    execution_agent: str = Field(default="general_agent", description="执行此任务的 agent")
    sort: int = Field(default=0, description="执行顺序序号")
    deps: list[str] = Field(default_factory=list, description="依赖的子任务 plan_id 列表")


class PlanOutput(BaseModel):
    """规划节点的结构化输出。"""

    action: str = Field(description="create: 创建全新计划（替换旧计划）；update: 更新现有计划状态；complete: 反思通过，直接给答案")
    title: str = Field(default="", description="计划标题")
    tasks: list[PlanTask] = Field(default_factory=list, description="子任务列表")
    answer: str = Field(default="", description="action=complete 时的最终答案文本，其他情况为空字符串")


async def _build_system_prompt(agent_descriptions: str = "", capability_descriptions: str = "") -> str:
    # Langfuse API 调用（网络 IO），异步
    langfuse = Langfuse()
    return await asyncio.to_thread(
        lambda: langfuse.get_prompt("plan_node_system_prompt", type="text").compile(
            agent_descriptions=agent_descriptions or "- general_agent: 通用执行 agent，可调用所有工具",
            capability_descriptions=capability_descriptions or "",
        )
    )


def _to_subtask(t: PlanTask) -> SubTask:
    """将 PlanTask 转换为 SubTask。"""
    return SubTask(
        plan_id=t.plan_id,
        name=t.name,
        desc=t.desc,
        execution_agent=t.execution_agent,
        sort=t.sort,
        deps=t.deps,
    )


async def plan_model_node(state: ThreadState, config: RunnableConfig, runtime: Runtime[GraphContext]) -> dict:
    context = runtime.context
    # llm = context.plan_llm
    # assert llm is not None, "plan_llm is required in GraphContext"
    writer = get_stream_writer()
    trace_id = trace_id_ctx_var.get()

    # 获取已有任务列表（review/replan 场景）
    existing_tasks = state.get("plan_tasks", [])
    writer({"type": THINK_MES, "messages": "📋 分析需求，制定执行计划...", "trace_id": trace_id})

    # 构建 system prompt
    from app.agents.tools import describe_execute_tools, get_plan_tools

    capability_desc = await describe_execute_tools()

    plan_context = ""
    if existing_tasks:
        plan_context = "\n".join(f"- [{t.step_statuses}] plan_id: {t.plan_id}: 任务名称: {t.name}: 执行结果：【{t.result if t.result else '待执行'}】" for t in existing_tasks)

    # 构建消息
    messages: list[BaseMessage] = []
    user_msgs = state.get("messages", [])
    messages.extend(user_msgs)
    context_lines = []
    if plan_context:
        context_lines.append(f"""<PlanStatus>\n当前计划
        {plan_context}\n\n
        </PlanStatus>""")
    if context_lines:
        messages.append(HumanMessage(content="\n".join(context_lines)))
    # 注入当前时间（供 agent 处理日期相关任务，如"今日天气"）
    # 若 state.messages 中已存在今天的 <current_time> 则不重复注入；
    # 只有不存在或已过期（非今天）时才追加一条新的当前时间消息。
    if not has_current_time_for_today(user_msgs):
        messages.append(HumanMessage(content=f"<current_time>{context.current_time}</current_time>"))

    # 捕获评估输入轨迹（规划 agent 实际看到的全部上下文，用于公平评估）
    eval_input = _capture_eval_input(
        messages=messages,
        plan_context=plan_context,
        current_time=context.current_time,
    )

    # 绑定工具：仅 ask_clarification（get_plan_tools 已包含）。
    plan_tools = await get_plan_tools()
    agent = create_agent(
        create_llm_with_name(config, model_name="plan_node_model"),
        plan_tools,
        middleware=[
            # 修复历史消息中"带 tool_calls 的 AIMessage 缺少对应 ToolMessage"的问题
            # （用户中断/压缩导致），否则 OpenAI 兼容 API 报 400。
            DanglingToolCallMiddleware(),
            ClarificationMiddleware(),
        ],
        name="plan_node_agent",
        response_format=PlanOutput,
        system_prompt=await _build_system_prompt(capability_descriptions=capability_desc),
    )

    # 规划节点重试机制：由 LangGraph 的 retry_policy（见 lead_agent/agent.py）接管。
    # 可恢复的 LLM 错误（超时/连接/5xx/429/服务繁忙）→ raise 交由 retry_policy 重试；
    # 欠费/认证失败等不可恢复错误 → 直接返回中文友好提示。
    try:
        # cast: create_agent 的输入类型是 _InputAgentState，实际传入 dict[str, list[BaseMessage]]
        # 是 langchain 标准用法，运行时安全；cast 消除静态类型噪音。
        agent_output = await agent.ainvoke(cast(Any, {"messages": messages}), config=config)
        agent_msgs: list[BaseMessage] = agent_output.get("messages", []) if isinstance(agent_output, dict) else []
        # 判定 agent 的实际输出类型（不管进哪个分支，都先评估）。
        # 注意：agent_output["messages"] 包含历史 + 本轮新增，若遍历全部，
        # 历史中曾出现过的 ask_clarification 调用会让 has_clarification 永远为 True
        # （用户已澄清后仍误判为"等待澄清"）。因此只检查「本轮新增」的消息。
        input_msg_ids = {getattr(m, "id", None) for m in messages if getattr(m, "id", None)}
        new_msgs = [m for m in agent_msgs if getattr(m, "id", None) not in input_msg_ids]
        has_clarification = any(isinstance(m, AIMessage) and getattr(m, "tool_calls", None) and any(tc.get("name") == "ask_clarification" for tc in m.tool_calls) for m in new_msgs)
        plan_output = _extract_plan_output(agent_output)

        # 填充评估输入，统一触发评估（覆盖澄清 / 规划 / 直接回复 全部分支）
        eval_input.clarification_requested = has_clarification
        if plan_output:
            eval_input.plan_action = plan_output.action
            eval_input.tasks = [t.model_dump() for t in plan_output.tasks]
        await _maybe_evaluate(
            trace_id=trace_id,
            eval_input=eval_input,
            messages=messages,
            config=config,
            runtime=runtime,
        )

        # 澄清：需求含糊时 agent 调用了 ask_clarification → 中断等待用户
        if has_clarification:
            writer({"type": THINK_MES, "messages": "📋 需要澄清需求", "trace_id": trace_id})
            return {"messages": agent_msgs, "completed": True}

        # 反思通过：action=complete 且有最终答案 → 作为 AIMessage 追加到 messages 作为回复
        if plan_output and plan_output.action == "complete" and plan_output.answer:
            answer_msg = AIMessage(content=plan_output.answer)
            writer({"type": THINK_MES, "messages": "📋 反思通过，生成最终答案", "trace_id": trace_id})
            # 反思通过后，清空旧计划（若有）。
            return {"messages": [answer_msg], "completed": True, "plan_tasks": Overwrite(value=[])}

        # 规划：模型输出了有效计划（有子任务）
        if plan_output and plan_output.tasks:
            subtasks = [_to_subtask(t) for t in plan_output.tasks]
            writer(
                {
                    "type": THINK_MES,
                    "messages": f"📋 规划完成，共 {len(subtasks)} 个子任务",
                    "task_count": len(subtasks),
                    "trace_id": trace_id,
                }
            )
            # action=create：创建全新计划，整体替换旧计划（无论有无旧任务）。
            # 提示词约束：create 仅用于「新问题与旧计划不一致」或「无旧计划」时，
            # 此时旧任务应全部废弃，用 Overwrite 绕过 merge reducer 整体替换。
            if plan_output.action == "create":
                return {"messages": agent_msgs, "plan_tasks": Overwrite(value=subtasks)}
            # action=update：合并到现有计划，保留旧任务
            return {"messages": agent_msgs, "plan_tasks": subtasks}

        # 模型直接给出了最终答案（answer 非空）但没有子任务：
        # 模型可能输出 action=create/update（未遵守「直接回复用 complete」约定），
        # 此时若把 agent_msgs 直接写回 state，前端会读到 ToolMessage 的
        # "Returning structured response: ..." 内部 dump。统一包装成干净 AIMessage。
        if plan_output and plan_output.answer:
            answer_msg = AIMessage(content=plan_output.answer)
            writer({"type": THINK_MES, "messages": "📋 反思通过，生成最终答案", "trace_id": trace_id})
            return {"messages": [answer_msg], "completed": True, "plan_tasks": Overwrite(value=[])}

        # 没有计划输出 → agent 直接回复（澄清、审查结论等）
        writer({"type": THINK_MES, "messages": "📋 规划完成", "trace_id": trace_id})
        return {"messages": agent_msgs, "completed": True}

    except Exception as e:
        retriable, reason = classify_llm_error(e)
        logger.error("Plan 节点失败 (reason={}): {}", reason, e, extra={"trace_id": trace_id})
        if retriable:
            # 可恢复错误（超时/连接/5xx/429/繁忙）：抛给 LangGraph retry_policy 重试
            raise
        # 不可恢复错误（欠费/认证/未知）：直接返回友好提示
        return {"messages": [build_error_fallback_message(e)], "completed": True}


def _extract_plan_output(agent_output: dict) -> PlanOutput | None:
    """从 agent 输出中提取结构化计划。

    兼容两种形态：
      1. 模型原生支持 structured output → PlanOutput 在最终 AIMessage 的 content/additional_kwargs 里
      2. 模型不支持（如 deepseek-v4-flash）→ LangChain fallback 到 tool-call 实现，
         真正解析结果存于 state 的 "structured_response" 字段，message 里只有
         "Returning structured response: ..." 的 ToolMessage
    """
    if not isinstance(agent_output, dict):
        return None

    # 1. 首选：fallback 模式（tool-call 实现）下的结构化响应
    structured = agent_output.get("structured_response")
    if structured is not None:
        if isinstance(structured, PlanOutput):
            return structured
        if isinstance(structured, dict):
            try:
                return PlanOutput.model_validate(structured)
            except Exception:
                pass

    # 2. 原生 JSON schema 模式：从 AIMessage content 解析
    messages = agent_output.get("messages", [])
    if not messages:
        return None
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            plan = _try_parse_plan(msg)
            if plan:
                return plan
    return None


def _try_parse_plan(msg: AIMessage) -> PlanOutput | None:
    """尝试从 AIMessage 解析 PlanOutput。"""

    # 1. 结构化输出注入到 content（JSON 字符串）
    content = getattr(msg, "content", None)
    if isinstance(content, str) and content.strip():
        try:
            return PlanOutput.model_validate_json(content)
        except Exception:
            pass

    # 2. additional_kwargs 里的 parsed
    try:
        kwargs = getattr(msg, "additional_kwargs", {}) or {}
        for key in ("parsed", "tool_call", "structured_output"):
            if key in kwargs:
                val = kwargs[key]
                if isinstance(val, dict):
                    return PlanOutput.model_validate(val)
    except Exception:
        pass

    return None


# ----------------------------------------------------------------------
# 规划评估（实验性）
# ----------------------------------------------------------------------


def _capture_eval_input(
    *,
    messages: list[BaseMessage],
    plan_context: str,
    current_time: str,
) -> EvaluationInput:
    """捕获规划节点实际看到的输入轨迹，供评估器公平判断。"""
    user_messages: list[str] = []
    history: list[dict] = []
    for m in messages:
        if isinstance(m, HumanMessage):
            content = m.content
            if isinstance(content, str) and content.strip():
                user_messages.append(content)
        history.append(
            {
                "type": type(m).__name__,
                "content": str(getattr(m, "content", ""))[:2000],
            }
        )
    return EvaluationInput(
        user_messages=user_messages,
        history=history,
        plan_status=plan_context,
        current_time=current_time,
    )


async def _maybe_evaluate(
    *,
    trace_id: str,
    eval_input: EvaluationInput,
    messages: list[BaseMessage],
    config: RunnableConfig,
    runtime: Runtime[GraphContext],
) -> None:
    """按配置触发规划评估；未配置、被禁用或被采样时静默跳过。"""
    try:
        context = runtime.context
        app_config = context.app_config

        # 优先用新式 evaluators 列表（name=plan_evaluation）；
        # 未配置时回退旧式 plan_evaluation 块（向后兼容）。
        eval_settings = app_config.get_evaluator("plan_evaluation")
        enabled = bool(eval_settings is not None and eval_settings.enabled)
        judge_model_name = eval_settings.model if eval_settings else None
        sample_rate = eval_settings.sample_rate if eval_settings else 1.0

        legacy_plan_eval = None
        if eval_settings is None:
            legacy_plan_eval = getattr(app_config, "plan_evaluation", None)
            enabled = bool(legacy_plan_eval is not None and getattr(legacy_plan_eval, "enabled", False))
            judge_model_name = getattr(legacy_plan_eval, "judge_model", None)
            sample_rate = getattr(legacy_plan_eval, "sample_rate", 1.0)
            if legacy_plan_eval is None or not enabled:
                return

        if not enabled:
            return

        # 允许的 execution_agent 集合：内置 general_agent + 配置的 custom_agents
        enabled_agents: set[str] = set()
        subagents_cfg = getattr(app_config, "subagents", None)
        if subagents_cfg is not None:
            custom = getattr(subagents_cfg, "custom_agents", {}) or {}
            enabled_agents = set(custom.keys())

        # Judge LLM：配置了 model / judge_model 就用它，否则用 plan_llm 兜底
        def _build_judge_llm(model_name: str | None) -> Any | None:
            if model_name:
                try:
                    return create_llm_with_name(config, model_name=model_name)
                except Exception:
                    return None
            return context.plan_llm

        # 新式 evaluators：走工厂（解析 use 子类 + 合并 metrics/系统提示词等）
        evaluator = None
        if eval_settings is not None:
            evaluator = create_evaluator(
                "plan_evaluation",
                app_config,
                llm_factory=lambda model_name: _build_judge_llm(model_name),
                langfuse=context.langfuse_client,
                extra={"enabled_agents": enabled_agents},
            )
        # 旧式 plan_evaluation 块：直接用 PlanEvaluator 构造
        else:
            from app.agents.evaluation.plan_evaluator import PlanEvaluator

            dimensions = dict(getattr(legacy_plan_eval, "dimensions", {}) or {})
            metrics = {name: {"enabled": val} for name, val in dimensions.items()}
            evaluator = PlanEvaluator(
                langfuse=context.langfuse_client,
                enabled_agents=enabled_agents,
                judge_llm=_build_judge_llm(judge_model_name),
                metrics=metrics or None,
                sample_rate=sample_rate,
            )

        if evaluator is None:
            return

        await evaluator.evaluate(
            trace_id=trace_id,
            prompt_input=evaluator.build_prompt_input(eval_input),
            messages=messages,
            config=config,
        )
    except Exception as exc:
        logger.warning("Plan evaluation skipped: %s", exc)
