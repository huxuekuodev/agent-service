"""Agent 配置（独立服务版简化）。

独立服务不加载自定义 agent 的 SOUL.md，仅保留 validate_agent_name 校验。
"""

from __future__ import annotations

import re

# 合法 agent 名：字母数字下划线短横线
AGENT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def validate_agent_name(name: str | None) -> str | None:
    """校验 agent 名（用于文件系统路径安全）。"""
    if name is None:
        return None
    if not isinstance(name, str):
        raise ValueError("Invalid agent name. Expected a string or None.")
    if not AGENT_NAME_PATTERN.fullmatch(name):
        raise ValueError(f"Invalid agent name '{name}'. Must match pattern: {AGENT_NAME_PATTERN.pattern}")
    return name


def load_agent_config(agent_name: str | None) -> None:
    """加载自定义 agent 配置。

    独立服务不支持自定义 agent，返回 None。
    """
    validate_agent_name(agent_name)
    return None
