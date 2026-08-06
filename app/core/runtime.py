"""运行时基础设施（独立服务版）。

简化版 RunContext：仅保留 GraphAgent 需要的字段。
原版 deerflow.runtime.RunContext 包含 checkpointer/store/event_store 等全套基础设施，
独立服务只使用 checkpointer（LangGraph 状态持久化）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RunContext:
    """单次 agent 运行的基础设施依赖。

    简化版：只保留 checkpointer（LangGraph 线程状态持久化）。
    """

    checkpointer: Any
    """LangGraph checkpointer（InMemorySaver / SqliteSaver 等）。"""

    app_config: Any | None = field(default=None)
    """应用配置（可选）。"""

    msg_history_pool: Any | None = field(default=None)
    """消息历史连接池（可选）。"""
