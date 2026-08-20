"""
规划节点评估器（LLM-as-Judge）。

对 plan_model_node 的规划输出做评估。基于 BaseEvaluator 抽象，通过 config.yaml
的 ``evaluators`` 列表注册（use: app.agents.evaluation.plan_evaluator:PlanEvaluator）。

Prompt 来源（唯一来源，不在代码中写死）：
  - Langfuse Prompt ``plan_evaluator_prompt``（文本类型，占位符
    ``{{prompt_input}}`` / ``{{output_schema}}``）。评估前必须在 Langfuse 中创建该 prompt。
  - Langfuse 不可用 / prompt 不存在时，评估静默跳过（不回落本地副本）。
  - 本地副本 ``app/prompts/plan_evaluator_prompt.md`` 仅作保存/参考，代码不读取。

评估指标（默认，与 Langfuse prompt 对齐，均 1-5 分，通过线 3.0）：
  1. task_atomicity（任务原子性）
  2. dependency_correctness（依赖关系正确性）
  3. plan_decision_accuracy（规划决策准确性：澄清 / create|update|complete / 反思）
  4. variable_reference_accuracy（变量引用准确性）
  5. tool_feasibility（工具可行性）

配置（config.yaml）：

    evaluators:
      - name: plan_evaluation
        display_name: "规划评估"
        use: app.agents.evaluation.plan_evaluator:PlanEvaluator
        model: evaluate_model
        enabled: true
        sample_rate: 1.0
        metrics:                       # 可选；按指标名覆盖开关 / 通过线
          task_atomicity:
            enabled: true
            pass_score: 3.0

Langfuse 衔接：
  - ``BaseEvaluator._emit_langfuse`` 用 ``start_observation(as_type="span",
    trace_context={"trace_id": ...})`` 在现有 trace 下创建兄弟 observation，
    并把各指标写成 Langfuse Score（``evaluation/plan_evaluation/{metric}``）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.agents.evaluation.base import BaseEvaluator, MetricConfig

logger = logging.getLogger(__name__)

__all__ = ["EvaluationInput", "PlanEvaluator", "maybe_evaluate_plan"]

#: 需要「有计划输出」才可评的指标（无任务时跳过，不计入 passed）
_TASK_METRICS = frozenset(
    {
        "task_atomicity",
        "dependency_correctness",
        "variable_reference_accuracy",
        "tool_feasibility",
    }
)


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
        """create / update / complete。"""
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
        "task_atomicity": MetricConfig(
            name="task_atomicity",
            label="任务原子性",
            pass_score=3.0,
            description="每个子任务是否只包含一个动作/一个步骤；存在复合任务按占比扣分。",
        ),
        "dependency_correctness": MetricConfig(
            name="dependency_correctness",
            label="依赖关系正确性",
            pass_score=3.0,
            description="任务间 deps 声明是否准确、完整、无环；无悬空/冗余/缺失依赖。",
        ),
        "plan_decision_accuracy": MetricConfig(
            name="plan_decision_accuracy",
            label="规划决策准确性",
            pass_score=3.0,
            description="澄清判断、create/update/complete 选择、反思决策三个关键决策点是否准确。",
        ),
        "variable_reference_accuracy": MetricConfig(
            name="variable_reference_accuracy",
            label="变量引用准确性",
            pass_score=3.0,
            description="后续任务 desc 中 {taskX} 引用是否与 deps 一致、格式正确、无编造或写死数据。",
        ),
        "tool_feasibility": MetricConfig(
            name="tool_feasibility",
            label="工具可行性",
            pass_score=3.0,
            description="每个任务是否能被执行阶段可用的工具/agent 实际完成。",
        ),
    }

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
            "history_text": history_text,
            "plan_status": eval_input.plan_status,
            "current_time": eval_input.current_time,
            "plan_action": eval_input.plan_action,
            "tasks_json": tasks_json,
            "clarification_requested": eval_input.clarification_requested,
        }

    def is_metric_applicable(self, name: str, prompt_input: dict[str, Any] | None = None) -> bool:
        """无计划输出（tasks 为空）时，任务类指标不适用（跳过，不计入 passed）；
        决策类指标在「有任务 / 请求澄清 / 有动作」任一情况下可评。"""
        prompt_input = prompt_input or {}
        if name in _TASK_METRICS:
            return bool(prompt_input.get("tasks"))
        if name == "plan_decision_accuracy":
            return bool(prompt_input.get("tasks") or prompt_input.get("clarification_requested") or prompt_input.get("plan_action"))
        return True

    # ------------------------------------------------------------------
    # LLM judge 组装（覆盖基类：直接使用 Langfuse 系统提示词 + 输入轨迹）
    # ------------------------------------------------------------------

    async def _llm_judge(
        self,
        *,
        trace_id: str,
        prompt_input: dict[str, Any],
        config: Any | None,
    ) -> tuple[dict[str, float], dict[str, str]]:
        """执行 LLM 评估。

        prompt 来源：Langfuse ``plan_evaluator_prompt``（文本类型，含
        ``{{prompt_input}}`` / ``{{output_schema}}`` 占位符），由 ``load_judge_prompt``
        渲染后整体作为一次对话输入（与基类单字符串 prompt 风格一致）。
        Langfuse 不可用 / prompt 不存在时跳过（返回空，不回落本地副本）。
        """
        if self._judge_llm is None:
            return {}, {}

        try:
            system_prompt = await self.load_judge_prompt(prompt_input=prompt_input)
            if not system_prompt:
                logger.warning("Plan evaluation skipped: prompt 'plan_evaluator_prompt' unavailable")
                return {}, {}

            response = await self._judge_llm.ainvoke(system_prompt, config=config)
            content = getattr(response, "content", None)
            raw = str(content) if content else ""
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.strip("`")
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            data = json.loads(raw)

            rationales: dict[str, str] = {}
            if isinstance(data, dict):
                rationale = data.get("rationale")
                if rationale is not None:
                    for name in self.get_metrics():
                        rationales[name] = str(rationale)
            return self.parse_llm_response(data), rationales
        except Exception as exc:
            logger.warning("LLM judge failed: %s", exc)
            return {}, {}

    # ------------------------------------------------------------------
    # Langfuse prompt 加载
    # ------------------------------------------------------------------

    @staticmethod
    def _render_prompt_input(prompt_input: dict[str, Any]) -> str:
        """把评估输入渲染成 ``{{prompt_input}}`` 的可读文本。"""
        lines = [
            f"- 用户消息: {json.dumps(prompt_input.get('user_messages', []), ensure_ascii=False)}",
            f"- 历史消息: {prompt_input.get('history_text', '')}",
            f"- 当前计划状态: {prompt_input.get('plan_status', '')}",
            f"- 当前时间: {prompt_input.get('current_time', '')}",
            f"- 规划动作: {prompt_input.get('plan_action', '')}",
            f"- 是否请求澄清: {prompt_input.get('clarification_requested', False)}",
            f"- 子任务列表: {prompt_input.get('tasks_json', '[]')}",
        ]
        return "\n".join(lines)

    async def load_judge_prompt(self, *, prompt_input: dict[str, Any]) -> str | None:
        """获取评估系统提示词：唯一来源为 Langfuse ``plan_evaluator_prompt``。

        prompt 必须预先在 Langfuse 创建（文本类型，占位符 ``{{prompt_input}}`` /
        ``{{output_schema}}``）。Langfuse 不可用 / prompt 不存在时返回 None（评估跳过）。
        本地副本 ``app/prompts/plan_evaluator_prompt.md`` 仅作保存参考，不在此读取。
        """
        if self._langfuse is None:
            logger.warning("Langfuse client unavailable; cannot load prompt 'plan_evaluator_prompt'")
            return None
        try:
            compiled = await asyncio.to_thread(
                lambda: self._langfuse.get_prompt("plan_evaluator_prompt", type="text").compile(
                    prompt_input=self._render_prompt_input(prompt_input),
                    output_schema=self._build_output_schema(),
                )
            )
            if compiled is None:
                return None
            if isinstance(compiled, list):
                # chat 类型 prompt：把各条消息 content 拼成一个字符串
                parts: list[str] = []
                for item in compiled:
                    if isinstance(item, dict):
                        parts.append(str(item.get("content", "")))
                    else:
                        parts.append(str(item))
                return "\n".join(parts)
            return str(compiled)
        except Exception as exc:
            logger.warning("Failed to load Langfuse prompt 'plan_evaluator_prompt': %s", exc)
            return None


# ----------------------------------------------------------------------
# 规划节点评估触发（供 plan_model_node 调用）
# ----------------------------------------------------------------------


async def maybe_evaluate_plan(
    *,
    trace_id: str,
    eval_input: EvaluationInput,
    messages: list[Any],
    config: Any,
    runtime: Any,
) -> None:
    """按配置触发规划评估；未配置、被禁用、被采样或 Langfuse 不可用时静默跳过。

    与 ``general_evaluator.maybe_evaluate_general`` 保持一致的触发模式：
    直接从 config 读 ``plan_evaluation`` 评估器设置，未配置则跳过（不引入旧配置兼容分支）。
    """
    try:
        from app.agents.evaluation.registry import create_evaluator
        from app.llm import create_llm_with_name

        context = runtime.context
        app_config = context.app_config

        eval_settings = app_config.get_evaluator("plan_evaluation")
        if eval_settings is None or not eval_settings.enabled:
            return

        def _build_judge_llm(model_name: str | None) -> Any | None:
            if not model_name:
                return None
            try:
                return create_llm_with_name(config, model_name=model_name)
            except Exception:
                return None

        evaluator = create_evaluator(
            "plan_evaluation",
            app_config,
            llm_factory=_build_judge_llm,
            langfuse=context.langfuse_client,
        )
        if evaluator is None:
            return

        await evaluator.evaluate(
            trace_id=trace_id,
            prompt_input=evaluator.build_prompt_input(eval_input),
            messages=messages,
            config=config,
        )
    except Exception as exc:
        logger.warning("Plan evaluation skipped: %s", exc)
