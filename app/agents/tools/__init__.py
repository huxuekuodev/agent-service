"""工具注册表（独立服务版，包化）。

按业务二分类组织第三方工具，供 agent 使用：

    app/agents/tools/
    ├── __init__.py          # 对外 API：get_plan_tools / get_execute_tools 等
    ├── registry.py          # 从 config.yaml `tools` 段加载工具类，按可用 agent 过滤
    ├── web/                 # 联网类工具（web_search 等）
    │   └── web_search.py
    ├── knowledge/           # 知识库类工具（私有/外部知识库检索等）
    │   └── internal_kb.py
    └── yuque/               # 语雀类工具（文档修订对比等）
        └── newest_doc.py

config.yaml 中的 ``tools`` 段声明每个工具：name / use（类 import 路径）/ category
（业务分类）/ allowed_agents（可用 agent 列表）/ enabled / extra（工具特有参数）。

plan 阶段只有 ask_clarification 工具；execute 阶段工具由配置的工具清单加载，
并按调用方 agent 名过滤（``allowed_agents`` 为空表示所有 agent 可用）。
"""

from __future__ import annotations

from typing import Any

from langchain.tools import BaseTool

from app.agents.tools.builtin import ask_clarification_tool
from app.agents.tools.registry import load_config_tools

__all__ = [
    "load_config_tools",
    "ask_clarification_tool",
    "get_plan_tools",
    "get_execute_tools",
    "describe_execute_tools",
    "describe_execute_tools_v2",
]


async def get_plan_tools(*, app_config: Any = None) -> list[BaseTool]:
    """获取 plan 阶段可用的工具（仅 ask_clarification）。"""
    return [ask_clarification_tool]


async def get_execute_tools(
    *,
    app_config: Any = None,
    agent: str = "general_agent",
) -> list[BaseTool]:
    """获取 execute 阶段可用的工具。

    从 config.yaml ``tools`` 段加载，按 ``allowed_agents`` 过滤出当前 agent 可用的工具。
    配置中无 ``tools`` 段时返回空列表（向后兼容）。
    """
    return load_config_tools(app_config=app_config, agent=agent)


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
