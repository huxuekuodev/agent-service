"""最终回复评估器：评估用户最终看到的那条答复（第三个触发点）。

前两个触发点看的是**过程**（规划是否合理、执行路径是否高效），这个触发点看的是**结果**：
答复是否正面回答了问题、内容是否有已执行结果支撑、信息是否完整。

这是"用户唯一的实际产出"，也是闭环里最该盯住的指标——过程再漂亮，答复答偏了就是失败。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from app.agents.evaluators.base import BaseEvaluator, MetricConfig, logger

__all__ = ["FinalAnswerEvaluator", "FinalAnswerEvaluationInput", "maybe_evaluate_final_answer"]


def _history_text(history: list[dict]) -> str:
    """把历史消息压成文本（只保留用户提问与最终答复，避免噪音）。"""
    lines: list[str] = []
    for item in history or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("type", ""))
        content = str(item.get("content", "") or "").strip()
        if not content:
            continue
        if "Human" in role:
            lines.append(f"[用户] {content}")
        elif "AI" in role:
            lines.append(f"[助手] {content}")
    return "\n".join(lines[-10:]) or "(无历史)"


@dataclass
class FinalAnswerEvaluationInput:
    """最终回复评估输入。"""

    def __init__(
        self,
        *,
        user_messages: list[str] | None = None,
        final_answer: str = "",
        task_results: list[dict] | None = None,
        plan_action: str = "",
        plan_status: str = "",
        history: list[dict] | None = None,
    ) -> None:
        self.user_messages = user_messages or []
        """用户的原始问题（本轮 + 历史）。"""
        self.final_answer = final_answer
        """待评估的最终答复。"""
        self.task_results = task_results or []
        """已执行任务的结果（含失败信息）——判断有据性的唯一依据。"""
        self.plan_action = plan_action
        self.plan_status = plan_status
        self.history = history or []


class FinalAnswerEvaluator(BaseEvaluator):
    """最终回复评估器（三个指标：问题契合度 / 结果有据性 / 完整性）。"""

    name = "final_answer_evaluation"

    default_system_prompt = "你是最终回复评估师，评估答复是否真正回答了用户的问题（只输出 JSON）。"

    default_metrics: dict[str, Any] = {
        "question_fit": MetricConfig(name="question_fit", label="问题契合度", description="是否正面回答用户真正问的问题", pass_score=3.0),
        "groundedness": MetricConfig(name="groundedness", label="结果有据性", description="答复内容是否有已执行任务结果支撑", pass_score=3.0),
        "completeness": MetricConfig(name="completeness", label="完整性", description="是否覆盖用户判断所需的信息要素", pass_score=3.0),
    }

    def build_prompt_input(self, eval_input: FinalAnswerEvaluationInput | dict | None = None, **kwargs: Any) -> dict[str, Any]:
        """把评估输入转成 prompt 填充字段。"""
        if eval_input is None:
            eval_input = FinalAnswerEvaluationInput(**kwargs)
        elif isinstance(eval_input, dict):
            eval_input = FinalAnswerEvaluationInput(**eval_input)

        return {
            "user_messages": eval_input.user_messages,
            "final_answer": eval_input.final_answer or "(空)",
            "task_results": json.dumps(eval_input.task_results, ensure_ascii=False, indent=2)[:4000] or "[]",
            "plan_action": eval_input.plan_action,
            "plan_status": eval_input.plan_status,
            "history_text": _history_text(eval_input.history),
        }

    # ------------------------------------------------------------------
    # LLM 评估（prompt 唯一来源：Langfuse final_answer_evaluator_prompt）
    # ------------------------------------------------------------------

    @staticmethod
    def _render_prompt_input(prompt_input: dict[str, Any]) -> str:
        """把评估输入渲染成 ``{{prompt_input}}`` 的可读文本。"""
        return "\n".join(
            [
                f"- 用户消息: {json.dumps(prompt_input.get('user_messages', []), ensure_ascii=False)}",
                f"- 最终答复: {prompt_input.get('final_answer', '')}",
                f"- 已执行任务结果: {prompt_input.get('task_results', '[]')}",
                f"- 规划动作: {prompt_input.get('plan_action', '')}",
                f"- 计划状态: {prompt_input.get('plan_status', '')}",
                f"- 历史消息: {prompt_input.get('history_text', '')}",
            ]
        )

    async def load_judge_prompt(self, *, prompt_input: dict[str, Any]) -> str | None:
        """获取系统提示词：唯一来源为 Langfuse ``final_answer_evaluator_prompt``。

        占位符 ``{{prompt_input}}`` / ``{{output_schema}}``；Langfuse 不可用/不存在时返回 None
        （评估跳过，不回落本地副本）。本地副本仅作保存参考。
        """
        if self._langfuse is None:
            logger.warning("Langfuse client unavailable; cannot load prompt 'final_answer_evaluator_prompt'")
            return None
        try:
            compiled = await asyncio.to_thread(
                lambda: self._langfuse.get_prompt("final_answer_evaluator_prompt", type="text").compile(
                    prompt_input=self._render_prompt_input(prompt_input),
                    output_schema=self._build_output_schema(),
                )
            )
            if compiled is None:
                return None
            if isinstance(compiled, list):
                return "\n".join(str(item.get("content", "")) if isinstance(item, dict) else str(item) for item in compiled)
            return str(compiled)
        except Exception as exc:
            logger.warning("Failed to load Langfuse prompt 'final_answer_evaluator_prompt': %s", exc)
            return None

    async def _llm_judge(
        self,
        *,
        trace_id: str,
        prompt_input: dict[str, Any],
        config: Any | None,
    ) -> tuple[dict[str, float], dict[str, str]]:
        """执行 LLM 评估（与另外两个评估器同风格：整段 prompt 作为一次对话输入）。"""
        if self._judge_llm is None:
            return {}, {}
        try:
            system_prompt = await self.load_judge_prompt(prompt_input=prompt_input)
            if not system_prompt:
                logger.warning("Final answer evaluation skipped: prompt 'final_answer_evaluator_prompt' unavailable")
                return {}, {}
            response = await self._judge_llm.ainvoke(system_prompt, config=config)
            raw = str(getattr(response, "content", "") or "").strip()
            if raw.startswith("```"):
                raw = raw.strip("`")
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            data = json.loads(raw)
            rationales: dict[str, str] = {}
            if isinstance(data, dict) and data.get("rationale") is not None:
                for name in self.get_metrics():
                    rationales[name] = str(data["rationale"])
            return self.parse_llm_response(data), rationales
        except Exception as exc:
            logger.warning("Final answer judge failed: %s", exc)
            return {}, {}

    def is_metric_applicable(self, name: str, prompt_input: dict[str, Any] | None = None) -> bool:
        """没有最终答复时三个指标都无从判断（避免把"没答复"混进分数分布）。"""
        prompt_input = prompt_input or {}
        return bool(str(prompt_input.get("final_answer", "")).strip() and str(prompt_input.get("final_answer")) != "(空)")


async def maybe_evaluate_final_answer(
    *,
    trace_id: str,
    eval_input: FinalAnswerEvaluationInput,
    messages: list[Any],
    config: Any,
    runtime: Any,
) -> None:
    """触发点 3：最终回复评估（异步，不阻塞对话）。

    判定点统一在 :func:`app.evaluation.decide_trigger`（阈值/开关在调用 agent 那层定）。
    """
    try:
        from app.agents.evaluators.registry import create_evaluator
        from app.evaluation import decide_trigger
        from app.llm import create_llm_with_name

        decision = decide_trigger("final_answer")
        if not decision.should:
            logger.debug("[evaluation] 跳过最终回复评估: %s", decision.reason)
            return

        context = runtime.context
        app_config = context.app_config
        eval_settings = app_config.get_evaluator("final_answer_evaluation")
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
            "final_answer_evaluation",
            app_config,
            llm_factory=_build_judge_llm,
            langfuse=context.langfuse_client,
        )
        if evaluator is None:
            return

        prompt_input = evaluator.build_prompt_input(eval_input)
        result = await evaluator.evaluate(
            trace_id=trace_id,
            prompt_input=prompt_input,
            messages=messages,
            config=config,
        )
        if result is not None and result.scores:
            from app.evaluation import record_evaluation, update_meta

            update_meta(judge_model=eval_settings.model or "")
            await record_evaluation(
                evaluator="FinalAnswerEvaluator",
                node="final_answer",
                trigger="final_answer",
                metric_scores=result.scores,
                rationales=result.rationales,
                passed=result.passed,
                judge_model=eval_settings.model or "",
                trace_id=trace_id,
                prompt_input=prompt_input,
                output_payload={"final_answer": eval_input.final_answer[:2000]},
                extra_meta={"policy_reason": decision.reason},
            )
    except Exception as exc:
        logger.warning("Final answer evaluation skipped: %s", exc)
