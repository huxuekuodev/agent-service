"""LLM 工厂：按「角色名 → 实例」从注册表构建 ChatModel。

角色名 = config.yaml ``models`` 段的 key（default / plan_node_model / ...）；
实例名 = app/llm/instances/ 中注册的 LLMInstance.name。
"""

from __future__ import annotations

from typing import Any

from langchain.chat_models import BaseChatModel

from app.core.reflection import resolve_class


def create_chat_model(
    name: str | None = None,
    thinking_enabled: bool = False,
    *,
    app_config: Any = None,
    attach_tracing: bool = True,
    **kwargs: Any,
) -> BaseChatModel:
    """按角色名创建 chat model 实例。

    Args:
        name: 角色名（config.yaml models 的 key）。None 时用默认角色。
        thinking_enabled: 是否启用 thinking（部分模型不支持，见实例 supports_thinking）。
        app_config: 应用配置（含 models 角色→实例映射）。
        attach_tracing: 是否附加 tracing callbacks（预留）。
        kwargs: 透传给模型类的额外参数（覆盖实例默认值）。

    Raises:
        ValueError: 角色未配置、实例未注册或模型类解析失败。
    """
    from app.config import get_app_config
    from app.llm.base import get_llm_instance

    config = app_config or get_app_config()
    role = name or config.default_model_name
    instance_name = config.models.get(role)
    if instance_name is None:
        raise ValueError(f"模型角色 {role!r} 未在 config.yaml 的 models 段配置")

    instance = get_llm_instance(instance_name)
    model_class = resolve_class(instance.use, BaseChatModel)

    # 构建模型参数：实例默认值 + 调用方覆盖
    model_kwargs: dict[str, Any] = {}
    if instance.api_key:
        model_kwargs["api_key"] = instance.api_key
    if instance.base_url:
        model_kwargs["base_url"] = instance.base_url
    model_kwargs["model"] = instance.model
    if instance.max_tokens is not None:
        model_kwargs["max_tokens"] = instance.max_tokens
    if instance.temperature is not None:
        model_kwargs["temperature"] = instance.temperature
    if instance.timeout is not None:
        model_kwargs["timeout"] = instance.timeout
    if instance.max_retries is not None:
        model_kwargs["max_retries"] = instance.max_retries
    model_kwargs.update(kwargs)

    return model_class(**model_kwargs)
