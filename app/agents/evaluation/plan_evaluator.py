"""
规划节点评估器（LLM-as-Judge）。

对 plan_model_node 的规划输出做评估。基于 BaseEvaluator 抽象，通过 config.yaml
的 ``evaluators`` 列表注册（use: app.agents.evaluation.plan_evaluator:PlanEvaluator）。

评估指标（默认，均 0-1，通过线 0.8）：
  1. clarification_quality：
     用户问题含糊/缺信息时，规划 agent 是否应调用 ask_clarification。
     评估器看到规划节点**实际收到的完整轨迹**（历史 + 当前计划状态），
     从而判断"此前的上下文是否已消歧"，避免事后上帝视角误判。
  2. task_atomicity：
     每个子任务是否是不可再分的原子步骤（无"先做A再做B"这种复合任务）。
  3. agent_selection_validity：
     每个子任务的 execution_agent 是否命中配置里真实存在的 agent。
     —— 这是**规则校验**（rule_based_metric），不需要 LLM。

配置（config.yaml）：

    evaluators:
      - name: plan_evaluation
        display_name: "规划评估"
        use: app.agents.evaluation.plan_evaluator:PlanEvaluator
        model: evaluate_model          # 可选；对应 models 列表里的 name
        system_prompt: |               # 可选；覆盖默认系统提示词
          你是规划质量评估器。...
        enabled: true
        sample_rate: 1.0
        metrics:                       # 可选；按指标名覆盖开关 / 通过线
          clarification_quality:
            enabled: true
            pass_score: 0.8
          task_atomicity:
            enabled: false

Langfuse 衔接：
  - 用 ``start_observation(as_type="span", trace_context={"trace_id": ...})``
    在**现有 trace 下**创建一个兄弟 observation（与 plan_agent span 并列）。
  - 每个指标用 ``span.score(...)`` 写为 Langfuse Scores。
"""

from __future__ import annotations

import json
from typing import Any

from app.agents.evaluation.base import BaseEvaluator, MetricConfig, MetricOutcome

__all__ = ["PlanEvaluator", "EvaluationInput"]

# 内置的执行 agent（与 SubTask.execution_agent 默认值一致）
_BUILTIN_EXECUTION_AGENTS = {"general_agent"}


class EvaluationInput:
    """规划评估输入（agent messages 历史 + 规划输出快照）。

    兼容两种构造方式：
      1. 关键字参数（与旧版本一致）。
      2. dict（``EvaluationInput(**data)``），便于 config/序列化场景。
    """

    def __init__(
        self,
        *,
        user_messages: list[str] | None = None,
        history: list[dict] | None = None,
        plan_status: str = "",
        current_time: str = "",
        plan_action: str = "",
        tasks: list[dict] | None = None,
        clarification_requested: bool = False,
    ) -> None:
        self.user_messages = user_messages or []
        """对话历史中的用户消息（用于澄清维度判断）。"""
        self.history = history or []
        """规划节点收到的完整消息轨迹（含 PlanStatus / current_time 注入消息）。"""
        self.plan_status = plan_status
        """注入到 agent 的 <PlanStatus> 内容（当前计划状态）。"""
        self.current_time = current_time
        """注入的当前时间。"""
        self.plan_action = plan_action
        """create / update。"""
        self.tasks = tasks or []
        """规划输出的子任务列表（已序列化）。"""
        self.clarification_requested = clarification_requested
        """本次规划 agent 是否调用了 ask_clarification。"""

    def to_dict(self) -> dict:
        return {
            "user_messages": self.user_messages,
            "history": self.history,
            "plan_status": self.plan_status,
            "current_time": self.current_time,
            "plan_action": self.plan_action,
            "tasks": self.tasks,
            "clarification_requested": self.clarification_requested,
        }


class PlanEvaluator(BaseEvaluator):
    """规划节点评估器。"""

    name = "plan_evaluation"

    default_metrics: dict[str, MetricConfig | dict[str, Any]] = {
        "clarification_quality": MetricConfig(
            name="clarification_quality",
            label="澄清质量",
            pass_score=0.8,
            description=(
                "需求含糊/缺信息 + agent 调用澄清 → 高分；需求含糊/缺信息 + agent 未澄清就强行规划或直接回复 → 低分；"
                "需求清晰 + agent 未澄清（直接规划）→ 高分；需求清晰但 agent 仍反复澄清 → 低分。"
                "判断必须结合<用户问题历史>：若历史中用户已提供足够信息（包括此前澄清的答案），则不应再澄清。"
            ),
        ),
        "task_atomicity": MetricConfig(
            name="task_atomicity",
            label="任务原子性",
            pass_score=0.8,
            description=("每个子任务是否是不可再分的原子步骤；存在复合任务（如'先搜索再总结'）按占比扣分。无任务输出（tasks 为空）时本指标不适用。"),
        ),
        "agent_selection_validity": MetricConfig(
            name="agent_selection_validity",
            label="执行 Agent 有效性",
            pass_score=0.8,
            description="每个子任务的 execution_agent 是否命中配置里真实存在的 agent。",
        ),
    }

    @property
    def default_system_prompt(self) -> str:
        return (
            "你是规划质量评估器。评估规划 agent 的决策质量，输出严格 JSON。\n"
            "规划 agent 可能的三种输出：\n"
            "1. 调用了 ask_clarification（clarification_requested=true）：需求含糊时澄清，是正确行为。\n"
            "2. 输出计划（tasks 非空）：需求清晰时拆解为原子任务。\n"
            "3. 直接回复（既未澄清也未产出计划）：若需求可执行却直接放弃，则是失败场景。"
        )

    @property
    def default_judge_prompt(self) -> str:
        return "你是规划质量评估器。评估规划 agent 的决策质量，输出严格 JSON。\n【评分标准】\n{metric_criteria}\n【输入】\n{prompt_input}\n【输出格式】严格 JSON，不要输出其他内容：\n{output_schema}"

    def __init__(
        self,
        *,
        enabled_agents: set[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._enabled_agents = (enabled_agents or set()) | _BUILTIN_EXECUTION_AGENTS

    # ------------------------------------------------------------------
    # BaseEvaluator 定制点
    # ------------------------------------------------------------------

    def build_prompt_input(self, eval_input: EvaluationInput | dict | None = None, **kwargs: Any) -> dict[str, Any]:
        """把评估输入转成 prompt 填充字段。"""
        if eval_input is None:
            eval_input = EvaluationInput(**kwargs)
        elif isinstance(eval_input, dict):
            eval_input = EvaluationInput(**eval_input)

        tasks_json = json.dumps(eval_input.tasks, ensure_ascii=False, indent=2)
        history_text = "\n".join(f"[{m.get('type')}] {m.get('content', '')}" for m in eval_input.history[-10:]) or "(无历史消息)"

        return {
            "user_messages": eval_input.user_messages,
            "history": eval_input.history[-10:],
            "history_text": history_text,
            "plan_status": eval_input.plan_status,
            "current_time": eval_input.current_time,
            "plan_action": eval_input.plan_action,
            "tasks": eval_input.tasks,
            "tasks_json": tasks_json,
            "clarification_requested": eval_input.clarification_requested,
        }

    def is_metric_applicable(self, name: str, prompt_input: dict[str, Any] | None = None) -> bool:
        """无计划输出（tasks 为空）时，任务类指标不适用（跳过，不计入 passed）。"""
        if name == "task_atomicity":
            prompt_input = prompt_input or {}
            return bool(prompt_input.get("tasks"))
        return True

    def rule_based_metric(self, name: str, prompt_input: dict[str, Any]) -> MetricOutcome | None:
        """agent_selection_validity：规则校验 execution_agent 是否命中配置集合，不走 LLM。"""
        if name != "agent_selection_validity":
            return None

        tasks = prompt_input.get("tasks") or []
        if not tasks:
            return MetricOutcome(score=1.0, rationale="无计划输出，跳过 agent 选择校验。")
        hallucinated: list[str] = []
        for t in tasks:
            agent = t.get("execution_agent", "general_agent")
            if agent not in self._enabled_agents:
                hallucinated.append(agent)
        if hallucinated:
            unique = list(dict.fromkeys(hallucinated))
            return MetricOutcome(
                score=0.0,
                rationale=f"检测到配置中不存在的 agent: {', '.join(unique)}。允许的 agent: {sorted(self._enabled_agents)}",
            )
        return MetricOutcome(score=1.0, rationale="所有 execution_agent 均在配置集合内。")
