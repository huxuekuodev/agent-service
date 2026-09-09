"""Skill 工具链（执行 agent 侧注入）。

    - list_skills        列出可用技能（skill_id + 何时使用）
    - load_skill         校验可用性并加载完整上下文（SOP/errors/cleanup 摘要）
    - skill_step_detail  取某 SOP 步骤的详细指令
    - run_skill_step     执行某步骤：脚本步骤 → E2B 沙箱；工具步骤 → 提示调用对应注册工具
    - query_error        按错误码查 errors.md 处置规则（失败恢复 / 人工介入决策用）

执行模型：skill 自带脚本一律进 E2B 沙箱运行（不落地本地环境）；
注册工具的步骤在本地执行（工具由执行 agent 注入，本工具链不代理）。
"""

from __future__ import annotations

from typing import Any

from langchain.tools import tool

from app.agents.skills.loader import load_skill_context_by_id
from app.agents.skills.registry import find_skill, scan_skills, validate_skill
from app.agents.skills.sandbox import SkillSandboxError, run_skill_script_once, sandbox_available


def _available_tool_names() -> set[str]:
    """已注册执行工具名集合（skill requires_tools 校验用，来自 config tools 段）。"""
    try:
        from app.agents.tools.registry import load_config_tools

        return {t.name for t in load_config_tools()}
    except Exception:
        return set()


@tool(parse_docstring=True)
async def list_skills() -> str:
    """List available skills (standard SOPs) that may fit the user's request.

    Call this before planning when the task looks like a repeatable procedure
    (e.g. doc review, report generation). Each skill carries a standard SOP;
    skill-owned scripts run inside an isolated sandbox, tool steps run locally.
    """
    metas = scan_skills()
    if not metas:
        return "（暂无可用技能）"
    return "\n".join(f"- {m.id}: {m.when_to_use or m.description}" for m in metas)


@tool(parse_docstring=True)
async def load_skill(skill_id: str) -> str:
    """Validate and load a skill's full context (SOP steps / error rules / cleanup rules).

    Args:
        skill_id: The skill id returned by list_skills.
    """
    meta = find_skill(skill_id)
    if meta is None:
        return f"技能不存在: {skill_id}。可用: {[m.id for m in scan_skills()]}"
    ok, missing = validate_skill(meta, _available_tool_names())
    ctx = await load_skill_context_by_id(skill_id)
    if ctx is None:
        return f"技能 {skill_id} 上下文加载失败（缺少 SOP/errors/cleanup 文件？）"
    lines = [f"技能「{skill_id}」可用性: {'可用' if ok else '缺工具: ' + ','.join(missing)}", ""]
    lines.append(ctx.sop_summary())
    if ctx.error_rules:
        lines += ["", "错误码索引:", ctx.error_index()]
    if ctx.cleanup_rules:
        lines += ["", "清理规则:", ctx.cleanup_summary()]
    return "\n".join(lines)


@tool(parse_docstring=True)
async def skill_step_detail(skill_id: str, step: str) -> str:
    """Get the detailed instruction of one SOP step of a skill.

    Args:
        skill_id: The skill id.
        step: Step id, e.g. step-01 (see load_skill output).
    """
    ctx = await load_skill_context_by_id(skill_id)
    if ctx is None:
        return f"技能不存在或上下文缺失: {skill_id}"
    for s in ctx.sop_steps:
        if s.step == step:
            mode = f"执行方式: {'沙箱脚本 ' + s.script if s.script else ('注册工具 ' + s.tool if s.tool else 'LLM 直接完成')}"
            return f"{step} {s.title}\n{mode}\n{s.description}"
    return f"步骤不存在: {skill_id}/{step}。可用: {[s.step for s in ctx.sop_steps]}"


@tool(parse_docstring=True)
async def run_skill_step(skill_id: str, step: str, arguments: list[str] | None = None) -> str:
    """Execute one SOP step of a skill: run its sandbox script, or instruct the local tool call.

    Args:
        skill_id: The skill id.
        step: Step id, e.g. step-01.
        arguments: CLI arguments for the script. When the step declares required
            arguments (args_hint), you MUST provide them here, e.g. ["--province", "河北省"].
    """
    ctx = await load_skill_context_by_id(skill_id)
    if ctx is None:
        return f"技能不存在或上下文缺失: {skill_id}"
    target = next((s for s in ctx.sop_steps if s.step == step), None)
    if target is None:
        return f"步骤不存在: {skill_id}/{step}"
    if target.tool:
        return f"该步骤应调用注册工具「{target.tool}」完成（本地执行），无需沙箱。"
    if not target.script:
        return f"步骤 {step} 无脚本/工具，由你（LLM）直接完成:\n{target.description}"
    # 脚本必需参数：缺参先引导，避免空跑输出 usage（浪费沙箱且误导）
    if target.args_required and not arguments:
        hint = target.args_hint or f"该脚本需要命令行参数，见步骤说明 {step}"
        return f"步骤 {step} 的脚本需要参数（脚本: {target.script}）。\n参数说明: {hint}\n请按需补齐 arguments 后重试（arguments 中的每一项依次作为脚本命令行参数）。"
    # 脚本步骤：必须沙箱执行
    available, reason = sandbox_available()
    if not available:
        return f"❌ 技能 step {step} 沙箱不可用: {reason}。请先完成沙箱配置再让用户重试，不要本地执行脚本。"
    try:
        result = await run_skill_script_once(ctx.meta.dir, target.script, arguments)
    except SkillSandboxError as exc:
        # 沙箱创建/连接等阶段错误（已分类带修复指引）
        return f"❌ 技能 step {step} 沙箱错误: {exc}"
    except Exception as exc:
        return f"❌ 技能 step {step} 执行异常: {exc}"
    if not result.ok:
        note = ""
        if target.args_hint and arguments:
            note = f"\n提示：若为参数错误，请核对参数格式。参数说明: {target.args_hint}"
        return f"❌ 技能 step {step} 脚本执行失败（exit_code={result.exit_code}）：\n{result.text}{note}"
    return result.text


@tool(parse_docstring=True)
async def query_error(skill_id: str, error_code: str) -> str:
    """Look up how to handle an error code from a skill's errors.md.

    Use when a skill step failed: get the recovery action (retry / build a
    recovery DAG / ask a human to confirm cleanup of produced temp files).

    Args:
        skill_id: The skill id.
        error_code: The error code observed (e.g. STEP_IO_ERROR).
    """
    ctx = await load_skill_context_by_id(skill_id)
    if ctx is None:
        return f"技能不存在或上下文缺失: {skill_id}"
    for rule in ctx.error_rules:
        if rule.code == error_code:
            return f"错误码 {rule.code}\n检测条件: {rule.condition}\n处置: {rule.action}\n说明/恢复: {rule.note or rule.recovery}"
    # 兜底：没有精确规则则展示全部
    return f"未找到错误码 {error_code} 的规则。该技能全部规则:\n" + (ctx.error_index() or "(空)")


def make_skill_tools() -> list[Any]:
    """返回技能工具链（供 get_execute_tools 追加）。"""
    return [list_skills, load_skill, skill_step_detail, run_skill_step, query_error]
