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
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from app.agents.evaluation.general_evaluator import maybe_evaluate_general
from app.agents.events import Output, StepStatus, ToolCallAccumulator
from app.agents.lead_agent import GraphContext
from app.agents.skills import is_skills_enabled, load_skill_context_by_id, sandbox_available, validate_skill
from app.agents.subtask import SubTask
from app.agents.thread_state import ThreadState
from app.agents.tools import describe_execute_tools_v2, get_execute_tools, load_config_tools
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
    # 技能任务：注入整技能执行流程（load_skill → sandbox_create → sandbox_run → 汇总 → sandbox_close）
    skill_guide = await _skill_task_guide(plan_id, plan_tasks)
    if skill_guide:
        task_info = f"{task_info}\n{skill_guide}"

    llm = create_llm_with_name(config, model_name="general_node_model")
    # create_agent 的 tools 参数会在内部自动 bind_tools，无需手动绑定
    agent = create_agent(model=llm, tools=await get_execute_tools(), system_prompt=system_prompt, name="general_node_agent")

    # 执行节点埋点 + 事件：step started / completed / failed（前端步骤卡片与进度）
    current = _find_task(plan_id, plan_tasks)
    task_skill_id = current.skill_id if current else ""
    out = Output("general_agent", trace_id=trace_id)
    out.think(f"▶️ 开始执行：{task_name}")
    out.step(plan_id=plan_id, name=task_name, status=StepStatus.STARTED, skill_id=task_skill_id)

    role = "general_node_model"
    start = dt.datetime.now().astimezone()
    await track(TrackingType.STEP_START, TrackingPage.EXECUTE, model=role, p0=plan_id, p1=task_name[:100])
    error_info = ""
    try:
        agent_msgs = await _run_execution_agent(agent, task_info, config=config, out=out, plan_id=plan_id)
        status = "completed"
    except Exception as exc:
        # 失败不裸抛中断全图：返回 failed 状态，交由规划节点按技能错误规则生成恢复 DAG 或人工介入
        status = "failed"
        error_info = str(exc)[:500]
        agent_msgs = []
        logger.warning("执行任务失败 (plan_id={}, trace_id={}): {}", plan_id, trace_id, exc)
    finally:
        duration_ms = int((dt.datetime.now().astimezone() - start).total_seconds() * 1000)
        await track(TrackingType.STEP_COMPLETE, TrackingPage.EXECUTE, model=role, p0=plan_id, p1=task_name[:100], p2=status, p3=str(duration_ms))

    if status == "failed":
        out.step(plan_id=plan_id, name=task_name, status=StepStatus.FAILED, detail=error_info or "执行失败")
        return {"plan_tasks": [SubTask(plan_id=plan_id, step_statuses="failed", blocked_message=error_info or "执行失败")]}

    final_msg = agent_msgs[-1] if agent_msgs else AIMessage(content="")
    task_result = final_msg.content if hasattr(final_msg, "content") else str(final_msg)
    out.step(plan_id=plan_id, name=task_name, status=StepStatus.COMPLETED, detail=str(task_result), skill_id=task_skill_id)

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


async def _run_execution_agent(agent: Any, task_info: str, *, config: RunnableConfig, out: Output, plan_id: str) -> list[BaseMessage]:
    """流式运行执行 agent：转发增量与工具调用事件，返回完整 transcript。

    transcript 以 ``updates`` 为准（含 tool_calls / ToolMessage），增量 chunk 只用于展示。
    """
    accumulator = ToolCallAccumulator()
    collected: dict[str, BaseMessage] = {}

    async for mode, payload in agent.astream({"messages": [HumanMessage(content=task_info)]}, config=config, stream_mode=["messages", "updates"]):
        if mode == "messages":
            chunk = payload[0] if isinstance(payload, (tuple, list)) and payload else payload
            out.thinking(_text_delta(chunk))
            for call in accumulator.feed(chunk):
                out.tool_call(name=call["name"], args=call["args"], plan_id=plan_id)
            continue

        if mode != "updates" or not isinstance(payload, dict):
            continue
        for update in payload.values():
            if not isinstance(update, dict):
                continue
            for msg in update.get("messages") or []:
                collected[getattr(msg, "id", None) or f"{type(msg).__name__}:{getattr(msg, 'content', '')}"] = msg
                if isinstance(msg, ToolMessage):
                    out.tool_result(name=msg.name or "", result=str(msg.content or ""), ok=str(getattr(msg, "status", "")) != "error", plan_id=plan_id)

    return list(collected.values())


def _text_delta(chunk: Any) -> str:
    """取流式 chunk 的文本增量（兼容 str 与 text-block 列表）。"""
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


async def _run_skill_probe(plan_tasks: list[SubTask]) -> str:
    """技能前置校验：技能可用性 + **沙箱环境**（执行前的唯一验证点）。

    结果注入下游任务，让执行 agent 一开始就知道：技能是否可用、脚本要在沙箱跑、
    以及整份技能的执行流程（SKILL.md 全文由执行 agent 用 load_skill 自己读）。
    """
    self_task = next((t for t in plan_tasks if t.plan_id == SKILL_PROBE_ID), None)
    skill_id = self_task.skill_id if self_task else ""
    if not skill_id:
        return "技能前置校验跳过：未声明 skill_id"
    try:
        ctx = await load_skill_context_by_id(skill_id)
        if ctx is None:
            return f"技能「{skill_id}」不存在或加载失败（缺少 SKILL.md？）"
        available = {t.name for t in load_config_tools()}
        ok, missing = validate_skill(ctx.meta, available)
        lines = [f"技能「{skill_id}」校验：{'可用' if ok else '缺工具: ' + ','.join(missing)}"]
        if ctx.meta.has_scripts:
            sb_ok, sb_reason = sandbox_available()
            lines.append(f"沙箱环境（自带脚本执行）: {'可用 ✅' if sb_ok else f'不可用 ❌ - {sb_reason}'}")
            if not sb_ok:
                lines.append("→ 下游任务必须如实报告沙箱不可用，不要本地执行脚本，也不要编造脚本输出。")
            else:
                lines.append(f"→ 下游任务执行流程: load_skill('{skill_id}') 读完整流程 → sandbox_create('{skill_id}') 准备环境 → sandbox_run('{skill_id}', '<命令>') 依次执行 → 汇总结果 → sandbox_close('{skill_id}')")
        else:
            lines.append("该技能无自带脚本，按 SKILL.md 的流程使用本地工具完成。")
        lines.append("")
        lines.append(f"技能文件清单:\n{ctx.file_index()}")
        if ctx.error_rules:
            lines += ["", "错误处置规则：", ctx.error_index()]
        if ctx.cleanup_rules:
            lines += ["", "产物清理规则：", ctx.cleanup_summary()]
        return "\n".join(lines)
    except Exception as exc:  # 校验失败不阻塞主流程，下游会看到失败说明
        logger.warning("技能前置校验异常 (skill_id=%s): %s", skill_id, exc)
        return f"技能「{skill_id}」前置校验异常: {exc}"


async def _skill_task_guide(plan_id: str, plan_tasks: list[SubTask]) -> str:
    """技能任务指引：告诉执行 agent 这是一次**完整技能执行**（读 SKILL → 准备沙箱 → 执行 → 汇总）。

    skills.enabled=false 时不注入。
    """
    if not is_skills_enabled():
        return ""
    task = _find_task(plan_id, plan_tasks)
    if task is None or not task.skill_id:
        return ""
    skill_id = task.skill_id
    try:
        ctx = await load_skill_context_by_id(skill_id)
        if ctx is None:
            return f"<技能任务>技能「{skill_id}」加载失败（缺少 SKILL.md）</技能任务>"
        lines = [
            "<技能任务>",
            f"本任务是一个完整技能的执行：`{skill_id}`（不要按步骤拆分，只做这一个技能）。",
            f"1. `load_skill('{skill_id}')` 读取整份 SKILL.md（完整流程、脚本用法、产出要求）；",
            f"2. `sandbox_create('{skill_id}')` 准备沙箱环境（自动同步 scripts/ data/ reference/），确认返回「环境已就绪」；",
            f"3. `sandbox_run('{skill_id}', '<命令>')` 按 SKILL.md 依次执行脚本（工作目录已是技能目录，用相对路径即可）；",
            "   命令失败时读 stderr/exit_code 自行修正参数后重试；必要时用 `query_error` 查处置规则；",
            f"4. 汇总脚本输出为任务结果；完成后 `sandbox_close('{skill_id}')` 释放沙箱。",
        ]
        if ctx.scripts:
            lines.append(f"技能自带脚本: {', '.join(ctx.scripts)}")
        if ctx.error_rules:
            lines.append(f"错误规则可用 query_error('{skill_id}', '<错误码>') 查询")
        lines.append("注意：sandbox_run 只能在沙箱内执行，禁止在本地运行技能脚本。")
        lines.append("</技能任务>")
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
    """从 app/prompts/ 读取通用执行节点提示词，替换 tools_desc 占位符（文件 IO 放线程池）。"""
    from pathlib import Path

    def _read() -> str:
        # __file__ = app/agents/nodes/general_agent.py → parents[2] = app/，提示词在 app/prompts/
        prompt_dir = Path(__file__).resolve().parents[2] / "prompts"
        path = prompt_dir / "general_agent_system_prompt.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    content = await asyncio.to_thread(_read)
    if content:
        return content.replace("{{tools_desc}}", tools_desc or "")
    return ("你是通用执行节点，负责完成分配的任务。\n可用工具:\n{tools_desc}\n请执行任务并返回结果。").format(tools_desc=tools_desc or "")
