"""评估子包。

核心抽象：
  - BaseEvaluator / BaseEvaluationResult / MetricConfig / MetricOutcome（base.py）
  - 注册表 / 工厂：create_evaluator / create_evaluator_from_settings（registry.py）
  - 内置评估器：PlanEvaluator（plan_evaluator.py）
"""

from app.agents.evaluation.base import BaseEvaluationResult, BaseEvaluator, MetricConfig, MetricOutcome
from app.agents.evaluation.general_evaluator import GeneralEvaluationInput, GeneralEvaluator, maybe_evaluate_general
from app.agents.evaluation.plan_evaluator import EvaluationInput, PlanEvaluator, maybe_evaluate_plan
from app.agents.evaluation.registry import create_evaluator, create_evaluator_from_settings

__all__ = [
    "BaseEvaluationResult",
    "BaseEvaluator",
    "MetricConfig",
    "MetricOutcome",
    "create_evaluator",
    "create_evaluator_from_settings",
    "EvaluationInput",
    "PlanEvaluator",
    "maybe_evaluate_plan",
    "GeneralEvaluationInput",
    "GeneralEvaluator",
    "maybe_evaluate_general",
]
