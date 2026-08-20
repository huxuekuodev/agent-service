"""LLM 构建器：按运行配置（RunnableConfig / 角色名）构建 ChatModel。

规划节点、执行节点、评估器统一从这里取 LLM（原 app/agents/lead_agent/llm.py 迁移而来）：
  - ``create_llm``：计划 + 执行通用 LLM（关闭 thinking，支持 structured output / tool binding）。
  - ``create_llm_with_name``：按角色名显式指定模型（如 plan_node_model / general_node_model / evaluate_model）。
  - ``create_execution_llm``：执行节点 LLM（开启 thinking，供工具调用推理）。
"""

from __future__ import annotations

import logging

from langchain_core.runnables import RunnableConfig

from app.config import AppConfig, get_app_config
from app.config.agents import load_agent_config, validate_agent_name
from app.llm.factory import create_chat_model

logger = logging.getLogger(__name__)


def _get_runtime_config(config: RunnableConfig) -> dict:
    """合并 legacy configurable 选项与 LangGraph runtime context。"""
    cfg = dict(config.get("configurable", {}) or {})
    context = config.get("context", {}) or {}
    if isinstance(context, dict):
        cfg.update(context)
    return cfg


def _resolve_model_name(requested_model_name: str | None = None, *, app_config: AppConfig | None = None) -> str:
    """安全解析运行时模型角色名，非法时回退默认角色。"""
    app_config = app_config or get_app_config()
    default_model_name = app_config.default_model_name
    if not default_model_name:
        raise ValueError("No chat models are configured. Please configure at least one model in config.yaml.")

    if requested_model_name and app_config.get_model_config(requested_model_name):
        return requested_model_name

    if requested_model_name and requested_model_name != default_model_name:
        logger.warning(f"Model role '{requested_model_name}' not found in config; fallback to default role '{default_model_name}'.")
    return default_model_name


def create_llm(config: RunnableConfig, *, app_config: AppConfig | None = None):
    """创建计划 + 执行 LLM。

    关闭 thinking 以支持 structured output 和 tool binding。
    """
    cfg = _get_runtime_config(config)
    resolved_app_config = app_config or get_app_config()

    is_bootstrap = cfg.get("is_bootstrap", False)
    requested_model_name: str | None = cfg.get("model_name") or cfg.get("model")
    agent_name = validate_agent_name(cfg.get("agent_name"))

    agent_config = load_agent_config(agent_name) if not is_bootstrap else None
    agent_model_name = agent_config.model if agent_config and agent_config.model else None
    model_name = _resolve_model_name(requested_model_name or agent_model_name, app_config=resolved_app_config)

    return create_chat_model(
        name=model_name,
        thinking_enabled=False,  # Thinking mode does not support tool_choice / structured output
        app_config=resolved_app_config,
        attach_tracing=False,
    )


def create_llm_with_name(config: RunnableConfig, *, app_config: AppConfig | None = None, model_name: str | None = None):
    """创建计划 + 执行 LLM（显式指定角色名）。

    关闭 thinking 以支持 structured output 和 tool binding。
    """
    resolved_app_config = app_config or get_app_config()

    return create_chat_model(
        name=model_name,
        thinking_enabled=False,  # Thinking mode does not support tool_choice / structured output
        app_config=resolved_app_config,
        attach_tracing=False,
    )


def create_execution_llm(config: RunnableConfig, *, app_config: AppConfig | None = None):
    """创建步骤执行用的 LLM（支持 thinking）。

    与 plan_llm 不同，执行 LLM 需要 thinking 能力来推理工具调用。
    """
    cfg = _get_runtime_config(config)
    resolved_app_config = app_config or get_app_config()

    requested_model_name: str | None = cfg.get("model_name") or cfg.get("model")
    agent_name = validate_agent_name(cfg.get("agent_name"))

    agent_config = load_agent_config(agent_name) if not cfg.get("is_bootstrap", False) else None
    agent_model_name = agent_config.model if agent_config and agent_config.model else None
    model_name = _resolve_model_name(requested_model_name or agent_model_name, app_config=resolved_app_config)

    return create_chat_model(
        name=model_name,
        thinking_enabled=True,  # 执行步骤需要 thinking
        app_config=resolved_app_config,
        attach_tracing=True,
    )
