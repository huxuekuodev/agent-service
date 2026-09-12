"""Skill 工具链（执行 agent 侧注入）。

    - ``list_skills``      列出可用技能（skill_id + 何时使用）
    - ``load_skill``       读取**整份技能**（SKILL.md 全文 + 参考文档 + 文件清单 + 错误/清理规则）
    - ``sandbox_create``   准备沙箱：环境校验 → 建沙箱 → 同步技能目录 → 报"环境就绪"
    - ``sandbox_run``      在已就绪的沙箱里执行命令（脚本/查看输出/自检），同一技能复用沙箱
    - ``sandbox_close``    销毁沙箱（未显式关闭时按空闲 TTL 自动回收）
    - ``sandbox_list``     查看当前存活的沙箱会话
    - ``query_error``      按错误码查技能错误处置规则

执行模型（**不拆分步骤**）：一个 skill 就是一个完整执行单元。执行 LLM 先 ``load_skill``
读完整份 SKILL.md（里面写着怎么跑、跑哪些脚本、产出什么），再 ``sandbox_create`` 准备环境，
用 ``sandbox_run`` 依次执行（脚本一律沙箱内运行），最后汇总结果并 ``sandbox_close``。
注册工具（``requires_tools`` 声明的）仍在**本地**执行，不进沙箱。
"""

from __future__ import annotations

from typing import Any

from langchain.tools import tool

from app.agents.skills.loader import load_skill_context_by_id
from app.agents.skills.registry import find_skill, scan_skills, validate_skill
from app.agents.skills.sandbox import SkillSandboxError, sandbox_available
from app.agents.skills.session import get_session_manager

#: 单次 load_skill 注入上下文的总字符上限（0 = 不截断）
_BUNDLE_MAX_CHARS = 60_000


def _available_tool_names() -> set[str]:
    """已注册执行工具名集合（skill requires_tools 校验用，来自 config tools 段）。"""
    try:
        from app.agents.tools.registry import load_config_tools

        return {t.name for t in load_config_tools()}
    except Exception:
        return set()


@tool(parse_docstring=True)
async def list_skills() -> str:
    """List available skills (standard procedures) that may fit the user's request.

    Each skill is a complete unit: read it with load_skill, prepare its sandbox with
    sandbox_create, then run its own scripts with sandbox_run.
    """
    metas = scan_skills()
    if not metas:
        return "（暂无可用技能）"
    lines = []
    for m in metas:
        scripts = "有自带脚本(需沙箱)" if m.has_scripts else "无自带脚本"
        lines.append(f"- {m.id}: {m.when_to_use or m.description} [{scripts}]")
    return "\n".join(lines)


@tool(parse_docstring=True)
async def load_skill(skill_id: str) -> str:
    """Read one complete skill: SKILL.md (full execution procedure) plus its reference docs, file inventory and error/cleanup rules.

    Call this first for any skill task. The returned procedure tells you which scripts
    to run and in what order; you decide the command sequence yourself (there is no
    step-by-step API).

    Args:
        skill_id: The skill id returned by list_skills.
    """
    meta = find_skill(skill_id)
    if meta is None:
        return f"技能不存在: {skill_id}。可用: {[m.id for m in scan_skills()]}"
    ctx = await load_skill_context_by_id(skill_id)
    if ctx is None:
        return f"技能 {skill_id} 加载失败（缺少 SKILL.md？）"

    ok, missing = validate_skill(meta, _available_tool_names())
    sb_ok, sb_reason = sandbox_available()

    lines = [
        f"技能「{skill_id}」可用性: {'可用' if ok else '缺工具: ' + ','.join(missing)}",
        f"沙箱（自带脚本执行环境）: {'可用' if sb_ok else '不可用 - ' + sb_reason}",
        "",
        ctx.bundle(max_chars=_BUNDLE_MAX_CHARS),
        "",
        f"# 文件清单（sandbox_create 会把整个技能目录同步进沙箱）\n{ctx.file_index()}",
    ]
    if ctx.error_rules:
        lines += ["", "# 错误处置规则（失败时用 query_error 查详情）", ctx.error_index()]
    if ctx.cleanup_rules:
        lines += ["", "# 产物清理规则", ctx.cleanup_summary()]
    lines += [
        "",
        "# 下一步",
        f"读完后调用 sandbox_create('{skill_id}') 准备沙箱环境，再用 sandbox_run('{skill_id}', '<命令>') 执行脚本。",
    ]
    return "\n".join(lines)


@tool(parse_docstring=True)
async def sandbox_create(skill_id: str, recreate: bool = False) -> str:
    """Prepare the skill sandbox: check the sandbox environment, start a sandbox and upload the whole skill directory (scripts/data/reference).

    Call this once before running any skill script. Repeated calls reuse the existing
    sandbox (fast); set recreate=True only after the skill files changed.

    Args:
        skill_id: The skill id.
        recreate: Destroy and rebuild the sandbox even if one is already running.
    """
    manager = get_session_manager()
    try:
        session = await manager.open(skill_id, recreate=recreate)
    except SkillSandboxError as exc:
        return f"❌ 沙箱准备失败: {exc}"
    except Exception as exc:
        return f"❌ 沙箱准备异常: {exc}"
    return (
        f"✅ 沙箱环境已就绪（技能 {skill_id}）\n"
        f"- 沙箱工作目录: {session.remote_dir}\n"
        f"- 已同步文件: {session.files} 个（scripts/ data/ reference/ 等）\n"
        f"- 执行方式: sandbox_run('{skill_id}', 'python3 scripts/xxx.py --arg value')，工作目录默认 {session.remote_dir}\n"
        f"- 完成后请调用 sandbox_close('{skill_id}') 释放沙箱"
    )


@tool(parse_docstring=True)
async def sandbox_run(skill_id: str, command: str, timeout_seconds: int | None = None) -> str:
    """Run a shell command inside the prepared skill sandbox (execute a script, inspect output, check data files).

    The working directory defaults to the skill directory inside the sandbox, so
    relative paths like scripts/get_city_code.py work directly. Output is stdout/stderr
    plus the exit code; a non-zero exit code means the command failed.

    Args:
        skill_id: The skill id.
        command: Full shell command, e.g. "python3 scripts/get_city_code.py --province 河北省".
        timeout_seconds: Optional per-command timeout (defaults to skills.sandbox.command_timeout).
    """
    manager = get_session_manager()
    try:
        session, result = await manager.run(skill_id, command, timeout=timeout_seconds, auto_open=True)
    except SkillSandboxError as exc:
        return f"❌ 沙箱执行失败: {exc}"
    except Exception as exc:
        return f"❌ 沙箱执行异常: {exc}"

    header = f"$ {command}\n(沙箱 {skill_id} @ {session.remote_dir}, exit_code={result.exit_code})"
    body = result.text
    if not body.strip():
        body = "(无输出)"
    if not result.ok:
        return f"{header}\n{body}\n提示: 命令执行失败，可参考技能错误规则（query_error）或检查参数/路径后重试。"
    return f"{header}\n{body}"


@tool(parse_docstring=True)
async def sandbox_close(skill_id: str) -> str:
    """Destroy the skill sandbox and release its resources (call this when the skill work is done).

    Args:
        skill_id: The skill id whose sandbox should be destroyed.
    """
    closed = await get_session_manager().close(skill_id)
    if not closed:
        return f"技能 {skill_id} 当前没有存活的沙箱（已释放或未创建）"
    return f"✅ 技能 {skill_id} 的沙箱已销毁（临时产物随之回收）"


@tool(parse_docstring=True)
async def sandbox_list() -> str:
    """List currently alive skill sandboxes (skill id, sandbox dir, command count, idle seconds)."""
    sessions = get_session_manager().list_sessions()
    if not sessions:
        return "（当前没有存活的沙箱会话）"
    return "\n".join(f"- {s['skill_id']}: {s['remote_dir']} 命令数={s['commands']} 空闲={s['idle_seconds']}s" for s in sessions)


@tool(parse_docstring=True)
async def query_error(skill_id: str, error_code: str) -> str:
    """Look up how to handle an error from a skill's errors.yaml (recovery action, human intervention, cleanup).

    Use this when a sandbox command failed and you are unsure how to proceed.

    Args:
        skill_id: The skill id.
        error_code: The error code observed (e.g. STEP_IO_ERROR); a short keyword from the failure also works.
    """
    ctx = await load_skill_context_by_id(skill_id)
    if ctx is None:
        return f"技能不存在或上下文缺失: {skill_id}"
    needle = (error_code or "").strip()
    for rule in ctx.error_rules:
        if rule.code == needle:
            return f"错误码 {rule.code}\n检测条件: {rule.condition}\n处置: {rule.action}\n说明/恢复: {rule.note or rule.recovery}"
    # 兜底：没有精确规则时按关键词模糊匹配，仍无则列出全部规则
    fuzzy = [r for r in ctx.error_rules if needle and (needle in r.code or needle in r.condition)]
    if fuzzy:
        return "\n\n".join(f"错误码 {r.code}\n检测条件: {r.condition}\n处置: {r.action}\n说明/恢复: {r.note or r.recovery}" for r in fuzzy)
    return f"未找到错误码 {needle} 的规则。该技能全部规则:\n" + (ctx.error_index() or "(空)")


def make_skill_tools() -> list[Any]:
    """返回技能工具链（供 get_execute_tools 追加）。"""
    return [list_skills, load_skill, sandbox_create, sandbox_run, sandbox_close, sandbox_list, query_error]
