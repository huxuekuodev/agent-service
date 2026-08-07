"""工具注册表（独立服务版）。

简化版：plan 阶段只有 ask_clarification 工具；
execute 阶段工具由配置的工具清单加载。
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from langchain.tools import BaseTool, tool

from app.core.reflection import resolve_variable

logger = logging.getLogger(__name__)


def _deduplicate_tools(tools: list[BaseTool]) -> list[BaseTool]:
    """去重。"""
    seen_names: set[str] = set()
    result: list[BaseTool] = []
    for t in tools:
        if t.name not in seen_names:
            result.append(t)
            seen_names.add(t.name)
    return result


def load_config_tools(
    stage: str | None = None,
    exact: bool = False,
    *,
    app_config: Any = None,
) -> list[BaseTool]:
    """从配置加载工具（极简：通过 resolve_variable 加载 use 路径）。

    当前独立服务暂未实现 config.yaml 工具清单的完整加载，
    默认返回空列表（执行 agent 通过 get_execute_tools 扩展）。
    """
    # TODO: 从 config.yaml / extensions 加载配置工具
    return []


@tool("ask_clarification", parse_docstring=True, return_direct=True)
def ask_clarification_tool(
    question: str,
    clarification_type: Literal[
        "missing_info",
        "ambiguous_requirement",
        "approach_choice",
        "risk_confirmation",
        "suggestion",
    ],
    context: str | None = None,
    options: list[str] | None = None,
) -> str:
    """Ask the user for clarification when you need more information to proceed.

    Use this tool when you encounter situations where you cannot proceed without user input:

    - **Missing information**: Required details not provided (e.g., file paths, URLs, specific requirements)
    - **Ambiguous requirements**: Multiple valid interpretations exist
    - **Approach choices**: Several valid approaches exist and you need user preference
    - **Risky operations**: Destructive actions that need explicit confirmation (e.g., deleting files, modifying production)
    - **Suggestions**: You have a recommendation but want user approval before proceeding

    The execution will be interrupted and the question will be presented to the user.
    Wait for the user's response before continuing.

    Args:
        question: The clarification question to ask the user. Be specific and clear.
        clarification_type: The type of clarification needed (missing_info, ambiguous_requirement, approach_choice, risk_confirmation, suggestion).
        context: Optional context explaining why clarification is needed. Helps the user understand the situation.
        options: Optional list of choices (for approach_choice or suggestion types). Present clear options for the user to choose from.
    """
    # 实际逻辑由 ClarificationMiddleware 拦截处理
    return "Clarification request processed by middleware"


async def get_plan_tools(*, app_config: Any = None) -> list[BaseTool]:
    """获取 plan 阶段可用的工具（ask_clarification）。"""
    return [ask_clarification_tool]


async def get_execute_tools(*, app_config: Any = None) -> list[BaseTool]:
    """获取 execute 阶段可用的工具。

    独立服务版：从配置加载执行工具，默认空。
    可通过 app_config 的 execute_tools 字段配置。
    """
    from app.config import get_app_config

    config = app_config or get_app_config()
    # TODO: 从配置加载执行工具（web_search 等）
    return []


async def describe_execute_tools(*, app_config: Any = None) -> str:
    """生成执行能力描述，供规划节点参考。"""
    tools = await get_execute_tools(app_config=app_config)
    if not tools:
        return ""

    lines = [
        "## 参考：执行阶段可用的工具（仅用于规划参考，你不可调用）",
        "",
        "以下工具将在步骤执行阶段可用。你应根据它们设计步骤。",
        "",
    ]
    for t in tools:
        name = t.name
        desc = t.description if hasattr(t, "description") else ""
        summary = desc.split("\n")[0].strip() if desc else name
        lines.append(f"- `{name}` — {summary}")
    return "\n".join(lines)


async def describe_execute_tools_v2(*, app_config: Any = None) -> str:
    """生成执行能力描述（v2，供执行 agent 参考）。"""
    tools = await get_execute_tools(app_config=app_config)
    if not tools:
        return ""

    lines = ["## 执行阶段可用的工具", ""]
    for t in tools:
        name = t.name
        desc = t.description if hasattr(t, "description") else ""
        summary = desc.split("\n")[0].strip() if desc else name
        lines.append(f"- `{name}` — {summary}")
    return "\n".join(lines)
