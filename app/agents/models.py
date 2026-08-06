"""模型工厂（独立服务版）。

替代 deerflow.models.factory，支持从配置实例化 LLM。
支持 thinking / vision 标志。
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
    """从配置创建 chat model 实例。

    Args:
        name: 模型名（对应 ModelConfig.name）。None 时用第一个模型。
        thinking_enabled: 是否启用 thinking。
        app_config: 应用配置（含 models 列表）。
        attach_tracing: 是否附加 tracing callbacks。
    """
    from app.config import get_app_config

    config = app_config or get_app_config()
    if name is None:
        name = config.default_model.name

    model_config = config.get_model_config(name)
    if model_config is None:
        raise ValueError(f"Model {name} not found in config")

    # 解析模型类
    model_class = resolve_class(model_config.use, BaseChatModel)

    # 构建模型参数
    model_kwargs: dict[str, Any] = {}
    if model_config.api_key:
        model_kwargs["api_key"] = model_config.api_key
    if model_config.base_url:
        model_kwargs["base_url"] = model_config.base_url
    if model_config.model:
        model_kwargs["model"] = model_config.model
    if model_config.max_tokens is not None:
        model_kwargs["max_tokens"] = model_config.max_tokens
    if model_config.temperature is not None:
        model_kwargs["temperature"] = model_config.temperature
    if model_config.timeout is not None:
        model_kwargs["timeout"] = model_config.timeout
    if model_config.max_retries is not None:
        model_kwargs["max_retries"] = model_config.max_retries

    # 合并额外 kwargs
    model_kwargs.update(kwargs)

    return model_class(**model_kwargs)
