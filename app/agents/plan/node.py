"""规划节点：澄清 → 规划 → 审查（一个节点内完成，产出经过校验的 SubTask DAG）。

职责（业务契约见 ``schema.py``，DAG 操作见 ``dag.py``，提交协议见 ``protocol.py``）：

1. **澄清**：问题不清晰 → 模型调 ``ask_clarification``，把问题呈现给用户；
2. **规划**：清晰 → 模型调 ``submit_plan`` 提交任务 DAG（技能任务在任务上标注 ``skill_id``）；
3. **审查**：任务执行完后回看 ``<PlanStatus>``，够回答就 ``action='complete'`` 直接给答案，
   不够就 ``action='update'`` 增补任务。

计划从模型到状态只走一条路：``submit_plan`` 的工具参数 → pydantic 校验（schema.py）
→ DAG 拓扑校验/修复（dag.py）→ ``plan_tasks`` 状态。没有"结构化输出三路兼容"这类分支。
"""

from dataclasses import dataclass, field
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command, Overwrite

from app.agents.common.current_time import has_current_time_for_today
from app.agents.common.errors import build_error_fallback_message, classify_llm_error
from app.agents.common.events import Output, ToolCallAccumulator
from app.agents.evaluators.plan_evaluator import EvaluationInput, maybe_evaluate_plan
from app.agents.graph import GraphContext
from app.agents.middlewares.clarification_middleware import ClarificationMiddleware
from app.agents.middlewares.dangling_tool_call_middleware import DanglingToolCallMiddleware
from app.agents.plan.dag import (
    INTERNAL_TASK_IDS,
    inject_skill_probe,
    render_plan_status,
    repair_topology,
    skill_ids_of,
    to_subtask,
    validate_topology,
)
from app.agents.plan.prompt import build_capability_desc, build_system_prompt
from app.agents.plan.protocol import SUBMIT_TOOL_NAME, PlanSubmissionMiddleware, parse_submission, submit_plan
from app.agents.plan.schema import SKILL_PROBE_ID, PlanOutput
from app.agents.state.subtask import SubTask
from app.agents.state.thread_state import ThreadState
from app.agents.tools import get_plan_tools
from app.core.context import trace_id_ctx_var
from app.core.log import logger
from app.core.tracking import TrackingPage, TrackingType
from app.core.tracking.tracker import track
from app.llm import create_llm_with_name


@dataclass
class PlanRun:
    """一次规划 agent 运行的结果（transcript + 提交的计划 + 澄清信息）。"""

    plan_output: PlanOutput | None = None
    new_messages: list[BaseMessage] = field(default_factory=list)
    """本轮新增消息（含 tool 消息），用于按需写回 state。"""
    all_messages: list[BaseMessage] = field(default_factory=list)
    """规划 agent 的完整 transcript（历史 + 新增），保 KV 前缀用。"""
    has_clarification: bool = False
    clarify_args: dict[str, Any] = field(default_factory=dict)
    """ask_clarification 的调用参数（问题/类型/选项），用于前端澄清卡片。"""


def _is_voice_mode() -> bool:
    """本次请求是否来自语音通话（决定输出风格）。"""
    from app.core.context import voice_mode_ctx_var

    return bool(voice_mode_ctx_var.get())


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


def _summarize_task_results(plan_tasks: list[SubTask], *, max_items: int = 5) -> str:
    """把已完成任务的结果汇总成答复（模型空输出时的兜底，排除系统内部任务）。"""
    useful = [t for t in plan_tasks if t.plan_id not in INTERNAL_TASK_IDS and t.result and t.result.strip() and t.step_statuses == "completed"]
    if not useful:
        return ""
    if len(useful) == 1:
        return useful[0].result.strip()
    lines = [f"- {t.name or t.plan_id}：{t.result.strip()}" for t in useful[:max_items]]
    extra = f"（另有 {len(useful) - max_items} 项未列出）" if len(useful) > max_items else ""
    return "已完成的内容如下：\n" + "\n".join(lines) + extra


def _dump_plan_output(plan_output: PlanOutput | None) -> str:
    """把计划压缩成一行日志（排障用）。"""
    if plan_output is None:
        return "None"
    try:
        return plan_output.model_dump_json()[:500]
    except Exception:  # pragma: no cover - 理论上不会失败
        return str(plan_output)[:500]


async def _build_messages(state: ThreadState, context: GraphContext, plan_context: str) -> list[BaseMessage]:
    """组装本轮规划 agent 的输入消息：历史 + （PlanStatus / 技能上下文）+ 当前时间。"""
    user_msgs = state.get("messages", [])
    messages: list[BaseMessage] = list(user_msgs)
    context_lines: list[str] = []
    if plan_context:
        context_lines.append(f"<PlanStatus>\n当前计划{plan_context}\n\n</PlanStatus>")
    if _is_voice_mode():
        # 语音通话：答复会被朗读，长答复=长等待，因此要求口语化短句
        context_lines.append("<VoiceMode>本次是语音通话，用户在用耳朵听：请用口语化中文回答，控制在 1-2 句、80 字以内；不要 Markdown、列表、表格、代码块与链接；需要多步操作时先说一句「我先去处理」，细节留到最终答复里简短带过。</VoiceMode>")
    if context_lines:
        messages.append(HumanMessage(content="\n".join(context_lines)))
    if not has_current_time_for_today(user_msgs):
        messages.append(HumanMessage(content=f"<current_time>{context.current_time}</current_time>"))
    return messages


# ----------------------------------------------------------------------
# 规划 agent：一次运行 = 澄清 / 提交计划 / 直接回答
# ----------------------------------------------------------------------


async def _run_plan_agent(config: RunnableConfig, messages: list[BaseMessage], out: Output) -> PlanRun:
    """运行规划 agent（流式消费），返回提交的计划与本轮 transcript。

    - ``messages`` 流：转发模型增量（打字机）+ 累积工具调用（实时展示执行动作）；
    - ``updates`` 流：收集 transcript（含 tool 消息）、捕获 ``submit_plan`` 提交与澄清参数。

    注意：transcript 与计划提交都以 ``updates`` 为准 —— 增量 chunk 不完整（会丢 tool_calls），
    工具参数也只有在完整 AIMessage 上才是可解析的。
    """
    agent = create_agent(
        create_llm_with_name(config, model_name="plan_node_model"),
        [*await get_plan_tools(), submit_plan],  # 计划提交是普通工具：不依赖任何渠道特性
        middleware=[DanglingToolCallMiddleware(), ClarificationMiddleware(), PlanSubmissionMiddleware()],
        name="plan_node_agent",
        system_prompt=await build_system_prompt(capability_descriptions=await build_capability_desc()),
    )

    run = PlanRun()
    accumulator = ToolCallAccumulator(ignore_names={SUBMIT_TOOL_NAME})  # 提交是内部协议，不作为"执行动作"展示
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
                if isinstance(msg, AIMessage):
                    _capture_submission(msg, run)
                if isinstance(msg, ToolMessage) and msg.name != SUBMIT_TOOL_NAME:
                    out.tool_result(name=msg.name or "", result=str(msg.content or ""), ok=str(getattr(msg, "status", "")) != "error")
                    if msg.name == "ask_clarification" and not run.clarify_args:
                        run.clarify_args = {"question": str(msg.content or "")}

    run.all_messages = list(collected.values())
    run.new_messages = [m for m in run.all_messages if getattr(m, "id", None) not in input_ids]
    run.has_clarification = any(isinstance(m, AIMessage) and any(tc.get("name") == "ask_clarification" for tc in (m.tool_calls or [])) for m in run.new_messages)
    if run.plan_output is None and not run.has_clarification:
        logger.warning("[plan] 本轮没有收到合法的 submit_plan 提交（模型未按协议提交计划）")
    return run


def _capture_submission(msg: AIMessage, run: PlanRun) -> None:
    """从 AIMessage 的 ``submit_plan`` 调用里取出计划（非法参数由工具节点回给模型修正）。"""
    for call in getattr(msg, "tool_calls", None) or []:
        if call.get("name") != SUBMIT_TOOL_NAME:
            continue
        plan = parse_submission(call.get("args"))
        if plan is not None:
            run.plan_output = plan  # 重复提交时以最后一次合法提交为准


# ----------------------------------------------------------------------
# 评估触发（触发点 3：最终回复）
# ----------------------------------------------------------------------


async def _trigger_final_answer_eval(
    *,
    answer: str,
    run: PlanRun,
    existing_tasks: list[SubTask],
    config: RunnableConfig,
    runtime: Runtime[GraphContext],
    trace_id: str,
) -> None:
    """**触发点 3**：最终回复评估（异步，不阻塞）。

    用户只看到最终答复，所以这是闭环里最关键的一次评估：答复是否答对问题、是否有据、是否完整。
    判定（阈值/开关）在调用层定，这里只负责把材料准备好交给评估器。
    """
    from app.agents.evaluators.final_answer_evaluator import FinalAnswerEvaluationInput, maybe_evaluate_final_answer

    user_messages = [str(m.content) for m in (run.all_messages or []) if isinstance(m, HumanMessage) and isinstance(m.content, str) and m.content.strip()]
    task_results = [{"plan_id": t.plan_id, "name": t.name, "status": t.step_statuses, "result": (t.result or "")[:1500], "blocked": t.blocked_message} for t in existing_tasks if t.plan_id != SKILL_PROBE_ID]
    await maybe_evaluate_final_answer(
        trace_id=trace_id,
        eval_input=FinalAnswerEvaluationInput(
            user_messages=user_messages[-3:],
            final_answer=answer,
            task_results=task_results,
            plan_action=(run.plan_output.action if run.plan_output else ""),
            plan_status=render_plan_status(existing_tasks),
            # 只给"用户可见"的对话（去掉空 AIMessage、结构化输出等内部消息——judge 会被它们误导）
            history=[{"type": type(m).__name__, "content": str(getattr(m, "content", ""))[:2000]} for m in (run.new_messages or []) if type(m).__name__ in ("HumanMessage", "AIMessage") and str(getattr(m, "content", "")).strip()],
        ),
        messages=run.all_messages or [],
        config=config,
        runtime=runtime,
    )


# ----------------------------------------------------------------------
# 落库 + 事件
# ----------------------------------------------------------------------


def _plan_subtasks(plan_output: PlanOutput, existing_tasks: list[SubTask], out: Output) -> list[SubTask]:
    """契约 → 运行期任务，并做 DAG 拓扑校验/修复（依赖指向不存在的任务会永久阻塞）。"""
    subtasks = [to_subtask(t) for t in plan_output.tasks]
    known_ids = {t.plan_id for t in existing_tasks} if plan_output.action == "update" else set()
    for problem in validate_topology(plan_output, known_ids):
        logger.warning("[plan] DAG 拓扑问题：{}", problem)
    subtasks, notes = repair_topology(subtasks, known_ids)
    for note in notes:
        out.think(f"⚠️ {note}")

    if plan_output.action == "create" or not any(t.plan_id == SKILL_PROBE_ID for t in existing_tasks):
        before = len(subtasks)
        subtasks = inject_skill_probe(subtasks)
        if len(subtasks) > before:
            out.think(f"📋 命中技能 {skill_ids_of(subtasks)[0]}，注入技能前置校验任务")
    return subtasks


async def _apply_plan_result(
    *,
    run: PlanRun,
    existing_tasks: list[SubTask],
    out: Output,
    config: RunnableConfig,
    trace_id: str,
    runtime: Runtime[GraphContext] | None = None,
) -> dict:
    """按规划结果落库并向前端发事件（澄清 / 最终答复 / 规划 / 直接回复）。"""
    plan_output = run.plan_output
    model = _request_model(config)

    # 1) 澄清：描述不清，等用户补充（transcript 以干净 AIMessage 回复用户）
    if run.has_clarification:
        clean_msgs = _clean_clarification_messages(run.new_messages)
        question = str(run.clarify_args.get("question") or "").strip()
        if not question:
            for m in reversed(clean_msgs):
                if isinstance(m, AIMessage) and str(m.content or "").strip():
                    question = str(m.content).strip()
                    break
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
        if runtime is not None:
            await _trigger_final_answer_eval(answer=plan_output.answer, run=run, existing_tasks=existing_tasks, config=config, runtime=runtime, trace_id=trace_id)
        return {"messages": [AIMessage(content=plan_output.answer)], "completed": True, "plan_tasks": Overwrite(value=[])}

    # 3) 规划：有子任务 → 注入技能探测（如需）后写回计划
    if plan_output and plan_output.tasks:
        subtasks = _plan_subtasks(plan_output, existing_tasks, out)
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
        if runtime is not None:
            await _trigger_final_answer_eval(answer=plan_output.answer, run=run, existing_tasks=existing_tasks, config=config, runtime=runtime, trace_id=trace_id)
        return {"messages": [AIMessage(content=plan_output.answer)], "completed": True, "plan_tasks": Overwrite(value=[])}

    # 5) 兜底：模型既没给答案、也没有新任务（例如审查轮返回空输出）
    #    直接结束会让用户"什么也看不到"（语音通话里就是"没有可播报的内容"），因此：
    #    - 有已完成任务的结果 → 汇总成答复（保证用户拿到东西，也保证语音有内容可播）；
    #    - 完全没有 → 记 warning 便于排查，并给一句诚实的提示而不是静默。
    summary = _summarize_task_results(existing_tasks)
    if summary:
        out.answer(content=summary)
        logger.warning("[plan] 模型未给出答复，已用已完成任务结果兜底（{} 字）", len(summary))
        if runtime is not None:
            await _trigger_final_answer_eval(answer=summary, run=run, existing_tasks=existing_tasks, config=config, runtime=runtime, trace_id=trace_id)
        return {"messages": [AIMessage(content=summary)], "completed": True, "plan_tasks": Overwrite(value=[])}

    logger.warning("[plan] 空输出且无可用结果（plan_output={}）", _dump_plan_output(plan_output))
    fallback = "这次没有产生可用的结果，请把问题再说清楚一些，或补充关键信息。"
    out.answer(content=fallback)
    if runtime is not None:
        await _trigger_final_answer_eval(answer=fallback, run=run, existing_tasks=existing_tasks, config=config, runtime=runtime, trace_id=trace_id)
    return {"messages": [AIMessage(content=fallback)], "completed": True, "plan_tasks": Overwrite(value=[])}


# ----------------------------------------------------------------------
# 节点入口
# ----------------------------------------------------------------------


async def plan_model_node(state: ThreadState, config: RunnableConfig, runtime: Runtime[GraphContext]) -> dict:
    """规划节点：澄清 → 规划 → 审查（一个节点内完成，输出结构化 PlanOutput）。"""
    context = runtime.context
    trace_id = trace_id_ctx_var.get()
    out = Output("plan_node", trace_id=trace_id)
    out.think("📋 分析问题中ing")

    existing_tasks = state.get("plan_tasks", [])
    plan_context = render_plan_status(existing_tasks)
    messages = await _build_messages(state, context, plan_context)
    eval_input = _capture_eval_input(messages=messages, plan_context=plan_context, current_time=context.current_time)

    # 节点级重试由 LangGraph retry_policy 接管（见 app/agents/graph/agent.py）：
    # 可恢复错误 → raise 重试；不可恢复（欠费/认证）→ 返回友好提示。
    try:
        run = await _run_plan_agent(config, messages, out)

        # 统一触发规划评估（覆盖澄清 / 规划 / 直接回复全部分支）
        eval_input.clarification_requested = run.has_clarification
        if run.plan_output:
            eval_input.plan_action = run.plan_output.action
            eval_input.tasks = [t.model_dump() for t in run.plan_output.tasks]
        # 身份与信号：评估结果要能归因（哪个任务/技能/prompt 版本），策略要能判断"值不值得评"
        from app.evaluation import update_meta
        from app.llm.builders import _resolve_model_name  # noqa: PLC0415  (运行时解析角色名)

        skill_ids = [t.skill_id for t in (run.plan_output.tasks if run.plan_output else []) if getattr(t, "skill_id", "")]
        update_meta(
            node="plan_node",
            run_model=_resolve_model_name(None, app_config=context.app_config),
            plan_id=(run.plan_output.tasks[0].plan_id if run.plan_output and run.plan_output.tasks else ""),
            skill_id=(skill_ids[0] if skill_ids else ""),
            signals={
                "replan": bool(existing_tasks),
                "clarify": bool(run.has_clarification),
                "task_count": len(run.plan_output.tasks) if run.plan_output else 0,
                "skill_used": bool(skill_ids),
                "first_turn": not bool(state.get("plan_tasks")),
                "failed_task": any(getattr(t, "step_statuses", "") == "failed" for t in existing_tasks),
            },
        )
        await maybe_evaluate_plan(trace_id=trace_id, eval_input=eval_input, messages=messages, config=config, runtime=runtime)

        return await _apply_plan_result(run=run, existing_tasks=existing_tasks, out=out, config=config, trace_id=trace_id, runtime=runtime)

    except Exception as e:
        retriable, reason = classify_llm_error(e)
        logger.error("Plan 节点失败 (reason={}): {}", reason, e, extra={"trace_id": trace_id})
        if retriable:
            raise
        message = build_error_fallback_message(e)
        # 错误提示：直接结束节点，不触发重试
        return Command(update={"messages": [message], "completed": True}, goto="END")


# ----------------------------------------------------------------------
# 辅助
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
