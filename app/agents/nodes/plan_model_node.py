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
from dataclasses import dataclass, field
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langfuse import Langfuse
from langgraph.runtime import Runtime
from langgraph.types import Overwrite
from pydantic import BaseModel, Field

from app.agents.current_time import has_current_time_for_today
from app.agents.errors import build_error_fallback_message, classify_llm_error
from app.agents.evaluation.plan_evaluator import EvaluationInput, maybe_evaluate_plan
from app.agents.events import Output, ToolCallAccumulator
from app.agents.lead_agent import GraphContext
from app.agents.middlewares.clarification_middleware import ClarificationMiddleware
from app.agents.middlewares.dangling_tool_call_middleware import DanglingToolCallMiddleware
from app.agents.subtask import SubTask
from app.agents.thread_state import ThreadState
from app.agents.tools import describe_execute_tools, get_plan_tools
from app.core.context import trace_id_ctx_var
from app.core.log import logger
from app.core.tracking import TrackingPage, TrackingType
from app.core.tracking.tracker import track
from app.llm import create_llm_with_name


# 构建 system prompt
class SkillChoice(BaseModel):
    """命中的技能候选（模型结构化输出）。"""

    skill_id: str = Field(description="技能 id（须来自 SkillsIndex 且可用），如 query-weather")
    reason: str = Field(default="", description="命中选择的理由")


class PlanTask(BaseModel):
    """计划中的单个子任务（模型结构化输出）。"""

    plan_id: str = Field(description="子任务唯一标识，如 task1 / task2")
    name: str = Field(description="子任务名称（简短）")
    desc: str = Field(description="子任务详细描述。可用 {其他任务plan_id} 引用依赖任务的结果")
    execution_agent: str = Field(default="general_agent", description="执行此任务的 agent")
    sort: int = Field(default=0, description="执行顺序序号")
    deps: list[str] = Field(default_factory=list, description="依赖的子任务 plan_id 列表")
    skill_id: str = Field(default="", description="该任务所属技能（按技能 SOP 拆解时填写，可选）")
    sop_step: str = Field(default="", description="该任务对应的技能 SOP 步骤，如 step-01（可选）")


class PlanOutput(BaseModel):
    """规划节点的结构化输出。"""

    action: str = Field(description="create: 创建全新计划（替换旧计划）；update: 更新现有计划状态；complete: 反思通过，直接给答案")
    title: str = Field(default="", description="计划标题")
    skills: list[SkillChoice] = Field(default_factory=list, description="命中的技能候选（≤3，主技能第一个）")
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
        skill_id=t.skill_id,
        sop_step=t.sop_step,
    )


#: 技能上下文探测任务的固定 plan_id（系统注入，模型不要创建）
SKILL_PROBE_ID = "skill_probe"


async def _skills_index_text() -> str:
    """导出 <SkillsIndex> 文本（附在能力描述末尾，供 plan 感知可用技能）。

    skills.enabled=false 时不注入（关闭技能链路，用于对比 token 消耗）。
    """
    from app.agents.skills import is_skills_enabled

    if not is_skills_enabled():
        return ""
    try:
        from app.agents.skills import skill_index_text
        from app.agents.tools.registry import load_config_tools

        available = {t.name for t in load_config_tools()}
        from app.config import get_app_config

        return skill_index_text(max_candidates=get_app_config().skills.max_candidates, available_tools=available)
    except Exception:
        return ""


def _inject_skill_probe(subtasks: list[SubTask], skill_ids: list[str]) -> list[SubTask]:
    """把「技能上下文探测」作为 DAG 首任务注入，并让业务任务依赖它。

    Args:
        subtasks: 模型产出的业务任务。
        skill_ids: 命中技能候选（主技能在前）。

    Returns:
        注入 skill_probe 后的任务列表（业务任务 deps 自动加 skill_probe）。
    """
    if not skill_ids:
        return subtasks
    primary = skill_ids[0]
    min_sort = min((t.sort for t in subtasks), default=0)
    probe = SubTask(
        plan_id=SKILL_PROBE_ID,
        name=f"校验并加载技能「{primary}」上下文",
        desc=f"[skill_probe] {primary}；候选 {skill_ids}；按 skill 工具链校验可用性并加载 SOP/错误/清理上下文，结果写回本任务",
        execution_agent="general_agent",
        sort=min_sort - 1,
        deps=[],
        skill_id=primary,
    )
    result: list[SubTask] = [probe]
    for t in subtasks:
        if not t.skill_id and len(skill_ids) == 1:
            t.skill_id = primary  # 单主技能时自动标注
        if SKILL_PROBE_ID not in t.deps:
            t.deps = [SKILL_PROBE_ID, *t.deps]
        result.append(t)
    return result


async def _skill_error_prompt(existing_tasks: list[SubTask]) -> list[str]:
    """从已有任务生成 <SkillErrors> 上下文块（供 review/恢复规划）。

    skills.enabled=false 时不注入技能上下文。
    """
    from app.agents.skills import is_skills_enabled

    if not is_skills_enabled():
        return []
    blocks: list[str] = []
    skill_ids = {t.skill_id for t in existing_tasks if t.skill_id}
    if not skill_ids:
        return blocks
    from app.agents.skills import load_skill_context_by_id

    errors: list[str] = []
    for sid in sorted(skill_ids):
        ctx = await load_skill_context_by_id(sid)
        if ctx is None:
            continue
        failed = [t for t in existing_tasks if t.skill_id == sid and t.step_statuses == "failed"]
        if failed:
            errors.append(f"技能 {sid} 错误处理规则：\n{ctx.error_index()}")

    if errors:
        blocks.append("<SkillErrors>\n" + "\n\n".join(errors) + "\n</SkillErrors>")
    return blocks


@dataclass
class PlanRun:
    """一次规划 agent 运行的结果（transcript + 结构化输出 + 澄清信息）。"""

    plan_output: PlanOutput | None = None
    new_messages: list[BaseMessage] = field(default_factory=list)
    """本轮新增消息（含 tool 消息），用于按需写回 state。"""
    all_messages: list[BaseMessage] = field(default_factory=list)
    """规划 agent 的完整 transcript（历史 + 新增），保 KV 前缀用。"""
    has_clarification: bool = False
    clarify_args: dict[str, Any] = field(default_factory=dict)
    """ask_clarification 的调用参数（问题/类型/选项），用于前端澄清卡片。"""


async def _build_capability_desc() -> str:
    """执行能力描述 + 技能索引（供规划节点感知可用工具与技能）。"""
    capability = await describe_execute_tools()
    skills_index = await _skills_index_text()
    if not skills_index:
        return capability
    return f"{capability}\n\n{skills_index}" if capability else skills_index


def _render_plan_status(existing_tasks: list[SubTask]) -> str:
    """把当前计划渲染成 <PlanStatus> 文本（review / replan 用）。"""
    return "\n".join(f"- [{t.step_statuses}] plan_id: {t.plan_id}: 任务名称: {t.name}: 执行结果：【{t.result or '待执行'}】" for t in existing_tasks)


async def _build_messages(state: ThreadState, context: GraphContext, plan_context: str) -> list[BaseMessage]:
    """组装本轮规划 agent 的输入消息：历史 + （PlanStatus / 技能上下文）+ 当前时间。"""
    user_msgs = state.get("messages", [])
    messages: list[BaseMessage] = list(user_msgs)
    context_lines: list[str] = []
    if plan_context:
        context_lines.append(f"<PlanStatus>\n当前计划{plan_context}\n\n</PlanStatus>")
    context_lines.extend(await _skill_error_prompt(state.get("plan_tasks", [])))
    if context_lines:
        messages.append(HumanMessage(content="\n".join(context_lines)))
    if not has_current_time_for_today(user_msgs):
        messages.append(HumanMessage(content=f"<current_time>{context.current_time}</current_time>"))
    return messages


def _message_key(msg: BaseMessage) -> str:
    """消息去重键（无 id 时退化为类型 + 内容）。"""
    return getattr(msg, "id", None) or f"{type(msg).__name__}:{getattr(msg, 'content', '')}"


def _text_of(chunk: Any) -> str:
    """取流式 chunk 的文本增量（兼容 str 与 text-block 列表）。"""
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _coerce_plan_output(structured: Any) -> PlanOutput | None:
    """把 structured_response 归一化为 PlanOutput。"""
    if isinstance(structured, PlanOutput):
        return structured
    if isinstance(structured, dict):
        try:
            return PlanOutput.model_validate(structured)
        except Exception:
            return None
    return None


async def _run_plan_agent(config: RunnableConfig, messages: list[BaseMessage], out: Output) -> PlanRun:
    """运行规划 agent（流式消费），返回结构化计划与本轮 transcript。

    - ``messages`` 流：转发模型增量（打字机）+ 累积工具调用（实时展示执行动作）；
    - ``updates`` 流：收集 transcript（含 tool 消息）、捕获结构化输出与澄清参数。

    注意：transcript 以 ``updates`` 为准，不能用增量 chunk 重建（会丢 tool_calls / ToolMessage）。
    """
    agent = create_agent(
        create_llm_with_name(config, model_name="plan_node_model"),
        await get_plan_tools(),
        middleware=[DanglingToolCallMiddleware(), ClarificationMiddleware()],
        name="plan_node_agent",
        response_format=PlanOutput,
        system_prompt=await _build_system_prompt(capability_descriptions=await _build_capability_desc()),
    )

    run = PlanRun()
    accumulator = ToolCallAccumulator(ignore_names={"PlanOutput"})  # 结构化输出是内部机制，不对用户展示
    collected: dict[str, BaseMessage] = {}
    input_ids = {m.id for m in messages if getattr(m, "id", None)}

    async for mode, payload in agent.astream(input={"messages": messages}, config=config, stream_mode=["messages", "updates"]):
        if mode == "messages":
            chunk = payload[0] if isinstance(payload, (tuple, list)) and payload else payload
            out.thinking(_text_of(chunk))
            for call in accumulator.feed(chunk):
                out.tool_call(name=call["name"], args=call["args"])
                if call["name"] == "ask_clarification":
                    run.clarify_args = call.get("args") or {}
            continue

        if mode != "updates" or not isinstance(payload, dict):
            continue
        for update in payload.values():
            if not isinstance(update, dict):
                continue
            for msg in update.get("messages") or []:
                collected[_message_key(msg)] = msg
                if isinstance(msg, ToolMessage):
                    out.tool_result(name=msg.name or "", result=str(msg.content or ""), ok=str(getattr(msg, "status", "")) != "error")
                    if msg.name == "ask_clarification" and not run.clarify_args:
                        run.clarify_args = {"question": str(msg.content or "")}
            structured = update.get("structured_response")
            if structured is not None:
                run.plan_output = _coerce_plan_output(structured) or run.plan_output

    run.all_messages = list(collected.values())
    run.new_messages = [m for m in run.all_messages if getattr(m, "id", None) not in input_ids]
    run.has_clarification = any(isinstance(m, AIMessage) and any(tc.get("name") == "ask_clarification" for tc in (m.tool_calls or [])) for m in run.new_messages)
    if run.plan_output is None:
        run.plan_output = _extract_plan_output({"messages": run.all_messages})
    return run


async def _apply_plan_result(
    *,
    run: PlanRun,
    existing_tasks: list[SubTask],
    out: Output,
    config: RunnableConfig,
    trace_id: str,
) -> dict:
    """按规划结果落库并向前端发事件（澄清 / 最终答复 / 规划 / 直接回复）。"""
    from app.agents.skills import is_skills_enabled

    plan_output = run.plan_output
    model = _request_model(config)

    # 1) 澄清：描述不清，等用户补充（transcript 以干净 AIMessage 回复用户）
    if run.has_clarification:
        clean_msgs = _clean_clarification_messages(run.new_messages)
        question = str(clean_msgs[-1].content) if clean_msgs else str(run.clarify_args.get("question", ""))
        out.clarify(
            content=question,
            clarification_type=str(run.clarify_args.get("clarification_type", "") or ""),
            options=list(run.clarify_args.get("options") or []) or None,
            context=str(run.clarify_args.get("context", "") or ""),
        )
        await track(TrackingType.CLARIFY, TrackingPage.PLAN, model=model, p4=question[:200])
        return {"messages": clean_msgs, "completed": True}

    # 2) 反思通过：直接给最终答案，并清空旧计划
    if plan_output and plan_output.action == "complete" and plan_output.answer:
        out.answer(content=plan_output.answer)
        await track(TrackingType.PLAN_COMPLETE, TrackingPage.PLAN, model=model, p2="complete", p4=plan_output.answer[:200])
        return {"messages": [AIMessage(content=plan_output.answer)], "completed": True, "plan_tasks": Overwrite(value=[])}

    # 3) 规划：有子任务 → 注入技能探测（如需）后写回计划
    if plan_output and plan_output.tasks:
        subtasks = [_to_subtask(t) for t in plan_output.tasks]
        skill_ids = [s.skill_id for s in plan_output.skills if s.skill_id] if is_skills_enabled() else []
        probe_exists = any(t.plan_id == SKILL_PROBE_ID for t in existing_tasks)
        if skill_ids and (plan_output.action == "create" or not probe_exists):
            subtasks = _inject_skill_probe(subtasks, skill_ids)
            out.think(f"📋 命中技能 {skill_ids[0]}，注入上下文探测任务")
        out.plan(action=plan_output.action, title=plan_output.title, tasks=[t.model_dump() for t in subtasks])
        out.think(f"📋 规划完成，共 {len(subtasks)} 个子任务")

        if plan_output.action == "create":
            await track(TrackingType.PLAN_CREATE, TrackingPage.PLAN, model=model, p1=str(len(subtasks)), p2="create")
            return {"messages": run.new_messages, "plan_tasks": Overwrite(value=subtasks)}
        await track(TrackingType.PLAN_UPDATE, TrackingPage.PLAN, model=model, p1=str(len(subtasks)), p2="update")
        return {"messages": run.new_messages, "plan_tasks": subtasks}

    # 4) 模型直接给了答案但没有子任务：包装成干净 AIMessage（避免内部 dump 上屏）
    if plan_output and plan_output.answer:
        out.answer(content=plan_output.answer)
        return {"messages": [AIMessage(content=plan_output.answer)], "completed": True, "plan_tasks": Overwrite(value=[])}

    # 5) 直接回复（澄清、审查结论等）
    out.think("📋 规划完成")
    return {"messages": run.new_messages, "completed": True}


async def plan_model_node(state: ThreadState, config: RunnableConfig, runtime: Runtime[GraphContext]) -> dict:
    """规划节点：澄清 → 规划 → 审查（一个节点内完成，输出结构化 PlanOutput）。"""
    context = runtime.context
    trace_id = trace_id_ctx_var.get()
    out = Output("plan_node", trace_id=trace_id)
    out.think("📋 分析需求，制定执行计划...")

    existing_tasks = state.get("plan_tasks", [])
    plan_context = _render_plan_status(existing_tasks)
    messages = await _build_messages(state, context, plan_context)
    eval_input = _capture_eval_input(messages=messages, plan_context=plan_context, current_time=context.current_time)

    # 节点级重试由 LangGraph retry_policy 接管（见 lead_agent/agent.py）：
    # 可恢复错误 → raise 重试；不可恢复（欠费/认证）→ 返回友好提示。
    try:
        run = await _run_plan_agent(config, messages, out)

        # 统一触发规划评估（覆盖澄清 / 规划 / 直接回复全部分支）
        eval_input.clarification_requested = run.has_clarification
        if run.plan_output:
            eval_input.plan_action = run.plan_output.action
            eval_input.tasks = [t.model_dump() for t in run.plan_output.tasks]
        await maybe_evaluate_plan(trace_id=trace_id, eval_input=eval_input, messages=messages, config=config, runtime=runtime)

        return await _apply_plan_result(run=run, existing_tasks=existing_tasks, out=out, config=config, trace_id=trace_id)

    except Exception as e:
        retriable, reason = classify_llm_error(e)
        logger.error("Plan 节点失败 (reason={}): {}", reason, e, extra={"trace_id": trace_id})
        if retriable:
            raise
        message = build_error_fallback_message(e)
        out.error(str(message.content))
        return {"messages": [message], "completed": True}


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
# 澄清消息清理
# ----------------------------------------------------------------------


def _request_model(config: RunnableConfig) -> str:
    """从运行配置取本次请求的模型角色名（打点 model 字段）。"""
    return str((config.get("configurable") or {}).get("model_name") or "")


def _clean_clarification_messages(agent_msgs: list[BaseMessage]) -> list[BaseMessage]:
    """把澄清的 [AIMessage(tool_calls) + ToolMessage] 对替换为一条干净的 AIMessage。

    ask_clarification 的问题由 ClarificationMiddleware 格式化后写入 ToolMessage，
    但 ToolMessage 是工具内部消息，不应作为对用户的回复；这里把问题内容封装成
    AIMessage 直接回复用户，并丢弃带 tool_calls 的 AIMessage：

    - 若只删 ToolMessage：下一轮会出现悬空 tool call，DanglingToolCallMiddleware
      会注入 "[Tool call was interrupted...]" 错误占位消息污染历史；
    - 若保留 ToolMessage 再追加 AIMessage：前端 streaming 会把两份相同内容都上屏。

    Returns:
        清理后的消息列表；未识别到澄清调用时原样返回。
    """
    ask_call_ids = {tc.get("id") for m in agent_msgs if isinstance(m, AIMessage) for tc in getattr(m, "tool_calls", None) or [] if tc.get("name") == "ask_clarification" and tc.get("id")}
    if not ask_call_ids:
        return agent_msgs

    clarification_text = ""
    kept: list[BaseMessage] = []
    for m in agent_msgs:
        if isinstance(m, ToolMessage) and m.tool_call_id in ask_call_ids:
            clarification_text = str(m.content or "")
            continue
        if isinstance(m, AIMessage):
            tool_calls = getattr(m, "tool_calls", None) or []
            if any(tc.get("name") == "ask_clarification" for tc in tool_calls):
                # 罕见情况：正文与澄清调用并存，保留正文
                if getattr(m, "content", None):
                    kept.append(AIMessage(content=str(m.content)))
                continue
        kept.append(m)
    if clarification_text:
        kept.append(AIMessage(content=clarification_text))
    return kept


# ----------------------------------------------------------------------
# 规划评估输入捕获
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
