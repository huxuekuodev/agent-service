"""
评估器注册表 / 工厂。

根据 config.yaml 的 ``evaluators`` 列表，把每个评估器配置实例化为
BaseEvaluator 子类对象：

  - ``use``: BaseEvaluator 子类的 import 路径（如 ``app.agents.evaluation.plan_evaluator:PlanEvaluator``）。
  - ``model``: 评估 LLM 在 ``models`` 列表里的 name；省略时用默认模型（即第一个）。
  - ``display_name``: 传给子类的 ``name``（Langfuse observation 名 ``evaluation/{name}``）。
  - ``system_prompt`` / ``metrics`` / ``sample_rate`` / ``extra``: 原样透传给子类 __init__。

用法：

    from app.agents.evaluation.registry import create_evaluator
    evaluator = create_evaluator("plan_evaluation", app_config, llm_factory=create_llm_with_name)
    result = await evaluator.evaluate(trace_id=..., prompt_input=..., config=...)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.agents.evaluation.base import BaseEvaluator
from app.config import AppConfig, EvaluatorSettings
from app.core.reflection import resolve_class

logger = logging.getLogger(__name__)


def create_evaluator(
    name: str,
    app_config: AppConfig,
    *,
    llm_factory: Callable[..., Any] | None = None,
    langfuse: Any | None = None,
    extra: dict[str, Any] | None = None,
) -> BaseEvaluator | None:
    """按名字创建评估器实例。

    Args:
        name: 评估器名（config.yaml evaluators.name）。
        app_config: 应用配置。
        llm_factory: 构建 LLM 的可调用对象，签名 ``(model_name: str | None) -> llm``；
            None 时尝试用 ``create_llm_with_name``。
        langfuse: Langfuse 客户端。
        extra: 额外覆盖参数（如 enabled_agents），合并进配置的 extra。

    Returns:
        评估器实例；未配置 / 被禁用 / use 解析失败时返回 None。
    """
    settings = app_config.get_evaluator(name)
    if settings is None:
        return None
    return create_evaluator_from_settings(
        settings,
        app_config=app_config,
        llm_factory=llm_factory,
        langfuse=langfuse,
        extra=extra,
    )


def create_evaluator_from_settings(
    settings: EvaluatorSettings,
    *,
    app_config: AppConfig,
    llm_factory: Callable[..., Any] | None = None,
    langfuse: Any | None = None,
    extra: dict[str, Any] | None = None,
) -> BaseEvaluator | None:
    """从 EvaluatorSettings 实例化评估器（供工厂内部与测试复用）。"""
    if not settings.enabled:
        return None
    if not settings.use:
        logger.warning("Evaluator '%s' has empty use path; skip", settings.name)
        return None

    # 解析子类
    try:
        cls = resolve_class(settings.use, BaseEvaluator)
    except Exception as exc:
        logger.error("Evaluator '%s': cannot resolve use='%s': %s", settings.name, settings.use, exc)
        return None

    # 构建 judge LLM
    judge_llm = None
    if llm_factory is not None:
        try:
            judge_llm = llm_factory(settings.model)
        except Exception as exc:
            logger.warning("Evaluator '%s': failed to build judge LLM (model=%s): %s", settings.name, settings.model, exc)

    # 实例化参数
    kwargs: dict[str, Any] = {
        "judge_llm": judge_llm,
        "system_prompt": settings.system_prompt,
        "metrics": settings.metrics or None,
        "langfuse": langfuse,
        "trace_name": f"evaluation/{settings.name}",
        "sample_rate": settings.sample_rate,
        "name": settings.display_name or settings.name,
    }
    kwargs.update(settings.extra or {})
    if extra:
        kwargs.update(extra)

    try:
        return cls(**kwargs)
    except Exception as exc:
        logger.error("Evaluator '%s': failed to instantiate %s: %s", settings.name, cls.__name__, exc)
        return None
