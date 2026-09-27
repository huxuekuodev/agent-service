"""规划提示词：从 Langfuse 取系统提示词，并把「执行能力 + 技能索引」注入进去。

提示词的唯一来源是 Langfuse（本地 ``app/prompts/plan_system_prompt_v2.md`` 是副本，
用 ``scripts/sync_langfuse_prompts.py`` 推送）。这里同时把**所用版本**写进评估身份
（``RunMeta.run_prompt_version``），"这版提示词分数有没有变好"才对比得出来。
"""

from __future__ import annotations

import asyncio

from langfuse import Langfuse

from app.agents.tools import describe_execute_tools

__all__ = ["build_capability_desc", "build_system_prompt", "skills_index_text"]

#: Langfuse 上的提示词名（与 scripts/sync_langfuse_prompts.py 中的 key 一致）
PLAN_PROMPT_KEY = "plan_node_system_prompt"

_FALLBACK_AGENTS = "- general_agent: 通用执行 agent，可调用所有工具"


async def build_system_prompt(agent_descriptions: str = "", capability_descriptions: str = "") -> str:
    """取规划节点系统提示词（并记录 prompt 版本，供评估归因）。"""
    langfuse = Langfuse()

    def _fetch() -> tuple[str, int]:
        prompt = langfuse.get_prompt(PLAN_PROMPT_KEY, type="text")
        return (
            prompt.compile(
                agent_descriptions=agent_descriptions or _FALLBACK_AGENTS,
                capability_descriptions=capability_descriptions or "",
            ),
            int(getattr(prompt, "version", 0) or 0),
        )

    text, version = await asyncio.to_thread(_fetch)
    from app.evaluation import update_meta

    update_meta(run_prompt_version=f"langfuse:v{version}" if version else "langfuse:unknown")
    return text


async def skills_index_text() -> str:
    """导出 ``<SkillsIndex>`` 文本（附在能力描述末尾，供规划感知可用技能）。

    ``skills.enabled=false`` 时不注入（关闭技能链路，用于对比 token 消耗）。
    """
    from app.agents.skills import is_skills_enabled

    if not is_skills_enabled():
        return ""
    try:
        from app.agents.skills import skill_index_text as render_skill_index
        from app.agents.tools.registry import load_config_tools
        from app.config import get_app_config

        available = {t.name for t in load_config_tools()}
        return render_skill_index(max_candidates=get_app_config().skills.max_candidates, available_tools=available)
    except Exception:
        return ""


async def build_capability_desc() -> str:
    """执行能力描述 + 技能索引（供规划节点感知可用工具与技能）。"""
    capability = await describe_execute_tools()
    index = await skills_index_text()
    if not index:
        return capability
    return f"{capability}\n\n{index}" if capability else index
