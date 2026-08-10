"""工具注册表：从 config.yaml ``tools`` 段加载工具，按可用 agent 过滤。

``tools`` 配置项字段（见 app/config/__init__.py 的 ToolConfig）：

    - name: 工具唯一名（即 agent 看到的工具名）。
    - use: 工具的可导入路径。可以指向三类目标：
        * 一个已实例化的 BaseTool 对象（如 ``app.agents.tools.web.web_search:web_search``）；
        * 一个工具工厂函数（接收 extra 关键字参数，返回 BaseTool）；
        * 一个 BaseTool 子类（用 extra 实例化）。
    - category: 业务分类（web / knowledge / ...），仅用于组织代码，不参与过滤。
    - allowed_agents: 可用 agent 名列表；为空表示所有 agent 可用。
    - enabled: 总开关。
    - extra: 透传给工具工厂/类的额外参数（如 API Key、endpoint 等）。

``load_config_tools`` 负责按 agent 名过滤：agent 不在 allowed_agents 内时，
该工具不会进入该 agent 的工具集。
"""

from __future__ import annotations

from typing import Any

from langchain.tools import BaseTool

from app.core.reflection import resolve_variable


def _deduplicate_tools(tools: list[BaseTool]) -> list[BaseTool]:
    """按工具名去重（重复时保留第一个）。"""
    seen: set[str] = set()
    result: list[BaseTool] = []
    for t in tools:
        if t.name not in seen:
            result.append(t)
            seen.add(t.name)
    return result


def _instantiate_tool(use: str, extra: dict[str, Any]) -> BaseTool:
    """实例化配置指向的工具。

    支持三类 ``use`` 目标：
    1. 已是 BaseTool 对象（``@tool`` 装饰的函数或 StructuredTool 实例）→ 直接返回；
    2. 工厂函数/可调用对象 → 以 ``extra`` 为关键字参数调用；
    3. BaseTool 子类 → 以 ``extra`` 实例化。

    Raises:
        ValueError: use 路径解析失败或结果不是 BaseTool。
    """
    target = resolve_variable(use)

    # 1. 已实例化的工具对象
    if isinstance(target, BaseTool):
        return target

    # 2/3. 工厂函数或类：先尝试带 extra，失败则无参调用
    tool_obj: Any = None
    if callable(target):
        try:
            tool_obj = target(**extra) if extra else target()
        except TypeError as err:
            if extra:
                raise ValueError(f"Tool factory {use!r} does not accept extra kwargs {extra!r}: {err}") from err
            raise

    if not isinstance(tool_obj, BaseTool):
        raise ValueError(f"Tool path {use!r} resolved to {type(tool_obj).__name__}, expected a BaseTool")
    return tool_obj


def load_config_tools(
    *,
    app_config: Any = None,
    agent: str | None = None,
) -> list[BaseTool]:
    """从配置加载工具，并按可用 agent 过滤。

    Args:
        app_config: AppConfig；缺省时用 get_app_config()。
        agent: 当前执行的 agent 名。工具在其 allowed_agents 非空且不包含
            agent 时被过滤掉。None 表示不过滤（加载全部启用工具）。

    Returns:
        过滤后的 BaseTool 列表。
    """
    from app.config import get_app_config

    config = app_config or get_app_config()

    tools: list[BaseTool] = []
    for tc in config.tools:
        if not tc.enabled:
            continue
        if agent and tc.allowed_agents and agent not in tc.allowed_agents:
            continue
        # 工具配置由用户在 config.yaml 保证正确；加载失败直接暴露问题（fail-fast）
        tool_obj = _instantiate_tool(tc.use, tc.extra)
        tools.append(tool_obj)
    return _deduplicate_tools(tools)
