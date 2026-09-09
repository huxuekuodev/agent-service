"""
通用执行 agent：接收 step_dispatch_node 通过 Send 派发的任务并执行。

流程：
  1. 从 state 获取 plan_id、task_name、task_desc（由 Send 注入）
  2. 从 plan_tasks 验证依赖任务是否已完成
  3. 调用 LLM 执行任务
  4. 修改任务状态为 completed，写入 result
"""

import asyncio
import datetime as dt

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from app.agents.evaluation.general_evaluator import maybe_evaluate_general
from app.agents.lead_agent import GraphContext
from app.agents.skills import (is_skills_enabled, load_skill_context_by_id,
                               sandbox_available, validate_skill)
from app.agents.subtask import SubTask
from app.agents.thread_state import ThreadState
from app.agents.tools import (describe_execute_tools_v2, get_execute_tools,
                              load_config_tools)
from app.core.context import trace_id_ctx_var
from app.core.log import logger
from app.core.tracking import TrackingPage, TrackingType
from app.core.tracking.tracker import track
from app.llm import create_llm_with_name

#: 技能上下文探测任务 plan_id（与 plan_model_node 保持一致）
SKILL_PROBE_ID = "skill_probe"


async def general_agent(state: ThreadState, config: RunnableConfig, runtime: Runtime[GraphContext]) -> dict:
    """通用执行 agent。

    任务信息（plan_id / task_name / task_desc）由 step_fan_out_router 通过 Send 注入。
    依赖验证和状态回写通过 plan_tasks（ThreadState 中）完成。

    Args:
        state: 合并了 Send 注入字段的 ThreadState
            - plan_id: 当前要执行的任务 ID
            - task_name: 任务名称
            - task_desc: 已注入依赖结果的任务描述

    Returns:
        更新后的 plan_tasks（标记 completed + result）。
    """
    plan_id = state.get("plan_id", "")
    task_name = state.get("task_name", "未知任务")
    task_desc = state.get("task_desc", "")
    plan_tasks = state.get("plan_tasks", [])
    # 优先从 config 取真实 trace_id（GraphAgent.astream 注入），兜底 ContextVar
    trace_id = (config.get("configurable") or {}).get("trace_id") or trace_id_ctx_var.get()

    if not plan_id:
        return {"completed": True}

    # === skill_probe：技能上下文探测（系统确定性执行，不走 LLM）===
    if plan_id == SKILL_PROBE_ID:
        return {"plan_tasks": [SubTask(plan_id=plan_id, step_statuses="completed", result=await _run_skill_probe(plan_tasks))]}

    # === 1. 验证依赖任务是否已完成 ===
    deps_results: str = ""
    for dep_id in _get_deps_of(plan_id, plan_tasks):
        dep_task = _find_task(dep_id, plan_tasks)
        if dep_task is None or dep_task.step_statuses != "completed":
            return {"plan_tasks": [SubTask(plan_id=plan_id, step_statuses="not_started", blocked_message=f"依赖任务 [{dep_id}] 尚未完成")]}
        deps_results += f"{dep_id}: {dep_task.result}\n"

    # === 2. 调用 LLM 执行任务 ===
    langfuse_client = runtime.context.langfuse_client
    tools_desc = await describe_execute_tools_v2()
    if langfuse_client is not None:
        try:
            system_prompt = langfuse_client.get_prompt("general_agent_system_prompt").compile(tools_desc=tools_desc)
        except Exception:
            system_prompt = await _load_local_general_prompt(tools_desc)
    else:
        system_prompt = await _load_local_general_prompt(tools_desc)
    task_info = f"""任务名称：{task_name}\n
                        任务描述：{task_desc}\n
                        计划 ID：{plan_id}\n
                        依赖任务结果：{deps_results}\n
                        <current_time>{runtime.context.current_time}</current_time>"""
    # 技能步骤任务：直接附上脚本/参数说明，避免执行 agent 因缺少参数而空跑脚本
    step_guide = await _skill_step_guide(plan_id, plan_tasks)
    if step_guide:
        task_info = f"{task_info}\n{step_guide}"

    llm = create_llm_with_name(config, model_name="general_node_model")
    # create_agent 的 tools 参数会在内部自动 bind_tools，无需手动绑定
    agent = create_agent(model=llm, tools=await get_execute_tools(), system_prompt=system_prompt, name="general_node_agent")

    # 执行节点埋点：step_start / step_complete（p0=plan_id, p1=任务名, p2=状态, p3=耗时ms）
    role = "general_node_model"
    start = dt.datetime.now().astimezone()
    await track(TrackingType.STEP_START, TrackingPage.EXECUTE, model=role, p0=plan_id, p1=task_name[:100])
    error_info = ""
    try:
        agent_result = await agent.ainvoke(
            {"messages": [HumanMessage(content=task_info)]},
            config=config,
        )
        status = "completed"
    except Exception as exc:
        # 失败不裸抛中断全图：返回 failed 状态，交由规划节点按技能错误规则生成恢复 DAG 或人工介入
        status = "failed"
        error_info = str(exc)[:500]
        logger.warning("执行任务失败 (plan_id={}, trace_id={}): {}", plan_id, trace_id, exc)
    finally:
        duration_ms = int((dt.datetime.now().astimezone() - start).total_seconds() * 1000)
        await track(TrackingType.STEP_COMPLETE, TrackingPage.EXECUTE, model=role, p0=plan_id, p1=task_name[:100], p2=status, p3=str(duration_ms))

    if status == "failed":
        return {"plan_tasks": [SubTask(plan_id=plan_id, step_statuses="failed", blocked_message=error_info or "执行失败")]}

    agent_msgs = agent_result.get("messages", [])
    final_msg = agent_msgs[-1] if agent_msgs else AIMessage(content="")
    task_result = final_msg.content if hasattr(final_msg, "content") else str(final_msg)

    # === 2.1 执行节点评估（LLM-as-Judge，非致命）===
    # 评估执行 agent 的工具调用路径效率；未配置 / 被禁用 / 失败时静默跳过，不影响主流程。
    try:
        await maybe_evaluate_general(
            trace_id=trace_id,
            task_info=task_info,
            messages=agent_msgs,
            tools_desc=tools_desc,
            current_time=runtime.context.current_time,
            config=config,
            runtime=runtime,
        )
    except Exception as exc:
        logger.warning("General evaluation skipped (trace_id={}): {}", trace_id, exc, extra={"trace_id": trace_id})

    # === 3. 修改任务状态为 completed ===
    return {"plan_tasks": [SubTask(plan_id=plan_id, step_statuses="completed", result=str(task_result))]}


async def _run_skill_probe(plan_tasks: list[SubTask]) -> str:
    """执行技能上下文探测：校验可用性 + 输出 SOP/错误/清理索引摘要（结果注入下游任务）。"""
    self_task = next((t for t in plan_tasks if t.plan_id == SKILL_PROBE_ID), None)
    skill_id = self_task.skill_id if self_task else ""
    if not skill_id:
        return "技能上下文探测跳过：未声明 skill_id"
    try:
        ctx = await load_skill_context_by_id(skill_id)
        if ctx is None:
            return f"技能「{skill_id}」不存在或上下文加载失败（缺少 SKILL.md？）"
        available = {t.name for t in load_config_tools()}
        ok, missing = validate_skill(ctx.meta, available)
        lines = [f"技能「{skill_id}」校验：{'可用' if ok else '缺工具: ' + ','.join(missing)}", ""]
        if ctx.meta.has_scripts:
            sb_ok, sb_reason = sandbox_available()
            lines.append(f"沙箱（自带脚本执行）: {'可用' if sb_ok else f'不可用 - {sb_reason}'}")
            lines.append("")
        lines.append(ctx.sop_summary())
        if ctx.error_rules:
            lines += ["", "错误码索引：", ctx.error_index()]
        if ctx.cleanup_rules:
            lines += ["", "清理规则：", ctx.cleanup_summary()]
        return "\n".join(lines)
    except Exception as exc:  # 探测失败不阻塞主流程，下游会看到失败说明
        logger.warning("技能上下文探测异常 (skill_id=%s): %s", skill_id, exc)
        return f"技能「{skill_id}」上下文探测异常: {exc}"


async def _skill_step_guide(plan_id: str, plan_tasks: list[SubTask]) -> str:
    """若当前任务带技能步骤标注，返回脚本/参数说明（追加进 task_info）。

    skills.enabled=false 时不注入技能指引。
    """
    if not is_skills_enabled():
        return ""
    task = _find_task(plan_id, plan_tasks)
    if task is None or not task.skill_id or not task.sop_step:
        return ""
    try:
        ctx = await load_skill_context_by_id(task.skill_id)
        if ctx is None:
            return ""
        step = next((s for s in ctx.sop_steps if s.step == task.sop_step), None)
        if step is None:
            return ""
        lines = ["<技能步骤指引>"]
        if step.script:
            lines.append(f'本任务执行技能脚本 {step.script}（沙箱运行），通过 run_skill_step("{task.skill_id}", "{task.sop_step}", arguments=[...]) 执行')
            if step.args_required:
                lines.append(f"脚本必需参数（arguments 依次为命令行参数）: {step.args_hint or '（见脚本用法）'}")
            else:
                lines.append("该脚本无需参数，arguments 可省略")
        elif step.tool:
            lines.append(f"本任务应调用注册工具「{step.tool}」完成（本地执行）")
        # 附上 SOP 步骤说明（含产出要求），避免执行 agent 丢弃关键字段（如城市编码）
        if step.description:
            lines.append(f"步骤说明: {step.description[:300]}")
        return "\n".join(lines)
    except Exception:
        return ""


def _find_task(plan_id: str, plan_tasks: list[SubTask]) -> SubTask | None:
    """从 plan_tasks 中查找指定 plan_id 的任务。"""
    return next((t for t in plan_tasks if t.plan_id == plan_id), None)


def _get_deps_of(plan_id: str, plan_tasks: list[SubTask]) -> list[str]:
    """获取指定任务的依赖列表。"""
    task = _find_task(plan_id, plan_tasks)
    return task.deps if task else []


async def _load_local_general_prompt(tools_desc: str) -> str:
    """从 app/prompts/ 读取通用执行节点提示词，替换 tools_desc 占位符（异步读文件）。"""
    from pathlib import Path

    async def _read() -> str:
        prompt_dir = Path(__file__).resolve().parent.parent.parent.parent / "prompts"
        path = prompt_dir / "general_agent_system_prompt.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    content = await asyncio.to_thread(_read)
    if content:
        # pyrefly: ignore [missing-attribute]
        return content.replace("{{tools_desc}}", tools_desc or "")
    return ("你是通用执行节点，负责完成分配的任务。\n可用工具:\n{tools_desc}\n请执行任务并返回结果。").format(tools_desc=tools_desc or "")
