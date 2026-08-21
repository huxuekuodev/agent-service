"""
通用执行节点评估器（LLM-as-Judge）。

对 general_agent 单次任务执行的「过程」做评估（不评估任务结果正确性，
那是执行 agent 自己的职责），基于 BaseEvaluator 抽象，通过 config.yaml
的 ``evaluators`` 列表注册（use: app.agents.evaluation.general_evaluator:GeneralEvaluator）。

评估指标（默认，1-5 分，通过线 3.0）：
  1. path_efficiency（路径效率）：
     评估执行 agent 本次任务的工具调用流程是否合理：是否存在冗余 / 重复 / 回退调用，
     是否在可用工具中选择了最优路径。评分标准见 Langfuse prompt ``general_evaluator_prompt``。

Prompt 来源（唯一来源，不在代码中写死）：
  - Langfuse Prompt ``general_evaluator_prompt``（文本类型，占位符
    ``{{history_messages}}`` / ``{{tools_desc}}``）。评估前必须在 Langfuse 中创建该 prompt。
  - Langfuse 不可用 / prompt 不存在时，评估静默跳过（不回落本地副本）。
  - 本地副本 ``app/prompts/general_evaluator_prompt.md`` 仅作保存/参考，代码不读取。

配置（config.yaml）：

    evaluators:
      - name: general_evaluation
        display_name: "执行评估"
        use: app.agents.evaluation.general_evaluator:GeneralEvaluator
        model: evaluate_model
        enabled: true
        sample_rate: 1.0
        metrics:
          path_efficiency:
            enabled: true
            pass_score: 3.0

Langfuse 衔接：
  - ``BaseEvaluator._emit_langfuse`` 用 ``start_observation(as_type="span",
    trace_context={"trace_id": ...})`` 在现有 trace 下创建兄弟 observation，
    并把 ``path_efficiency`` 写成 Langfuse Score（``evaluation/general_evaluation/path_efficiency``）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.agents.evaluation.base import BaseEvaluator, MetricConfig

logger = logging.getLogger(__name__)

__all__ = ["GeneralEvaluationInput", "GeneralEvaluator", "maybe_evaluate_general"]


def _serialize_message(msg: Any) -> dict:
    """序列化一条 LangChain 消息为可读 dict（含工具调用轨迹）。"""
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        content = " ".join(parts)
    d: dict[str, Any] = {
        "type": type(msg).__name__,
        "content": str(content or ""),
        "name": getattr(msg, "name", None),
    }
    tool_calls = getattr(msg, "tool_calls", None) or []
    if tool_calls:
        d["tool_calls"] = [
            {
                "name": tc.get("name"),
                "args": tc.get("args"),
                "id": tc.get("id"),
            }
            for tc in tool_calls
        ]
    return d


def _serialize_tool_messages(messages: list[Any]) -> list[dict]:
    """把 LangChain 消息列表序列化成评估输入用的历史消息（含工具调用轨迹）。"""
    return [_serialize_message(m) for m in messages]


class GeneralEvaluationInput:
    """执行节点评估输入。

    兼容两种构造方式：
      1. 关键字参数（与 plan_evaluator 风格一致）。
      2. dict（``GeneralEvaluationInput(**data)``），便于 config / 序列化场景。
    """

    def __init__(
        self,
        *,
        task_info: str = "",
        history: list[dict] | None = None,
        tools_desc: str = "",
        current_time: str = "",
    ) -> None:
        self.task_info = task_info
        """任务描述（任务名称 / 任务描述 / 依赖结果 / 当前时间）。"""
        self.history = history or []
        """执行 agent 收到的完整消息轨迹（含 AIMessage / ToolMessage / 最终回复）。"""
        self.tools_desc = tools_desc
        """执行 agent 可用工具的说明（供评估器判断路径选择）。"""
        self.current_time = current_time
        """当前时间。"""

    def to_dict(self) -> dict:
        return {
            "task_info": self.task_info,
            "history": self.history,
            "tools_desc": self.tools_desc,
            "current_time": self.current_time,
        }


class GeneralEvaluator(BaseEvaluator):
    """通用执行节点评估器。"""

    name = "general_evaluation"

    default_metrics: dict[str, MetricConfig | dict[str, Any]] = {
        "path_efficiency": MetricConfig(
            name="path_efficiency",
            label="路径效率",
            pass_score=3.0,
            description=("评估执行 agent 的工具调用流程是否合理：是否存在冗余 / 重复 / 回退调用，是否在可用工具中选择了最优路径。1-5 分，通过线 3 分（有轻微瑕疵但整体可接受）。"),
        ),
    }

    # ------------------------------------------------------------------
    # BaseEvaluator 定制点
    # ------------------------------------------------------------------

    def build_prompt_input(self, eval_input: GeneralEvaluationInput | dict | None = None, **kwargs: Any) -> dict[str, Any]:
        """把评估输入转成 prompt 填充字段。"""
        if eval_input is None:
            eval_input = GeneralEvaluationInput(**kwargs)
        elif isinstance(eval_input, dict):
            eval_input = GeneralEvaluationInput(**eval_input)

        return {
            "task_info": eval_input.task_info,
            "history": eval_input.history,
            "history_text": self._format_history(eval_input.history),
            "tools_desc": eval_input.tools_desc,
            "current_time": eval_input.current_time,
        }

    @staticmethod
    def _format_history(history: list[dict]) -> str:
        """把历史消息序列化成评估 prompt 中 ``{{history_messages}}`` 的填充文本。

        保留与工具调用轨迹相关的消息（AIMessage 及其 tool_calls、ToolMessage、最终回复），
        剔除冗余的注入消息（<current_time> 等）与系统内部消息，避免干扰路径效率判断。
        """
        lines: list[str] = []
        for m in history:
            mtype = m.get("type", "")
            content = str(m.get("content", "") or "").strip()
            tool_calls = m.get("tool_calls") or []

            if mtype == "AIMessage":
                if tool_calls:
                    for tc in tool_calls:
                        tc_name = tc.get("name", "")
                        tc_args = json.dumps(tc.get("args", {}), ensure_ascii=False)
                        lines.append(f"[工具调用] {tc_name}({tc_args})")
                    # 纯 tool-call 消息的 content 通常为空 / "请调用工具" 等，不单独输出
                    if content and content not in ("", "请调用工具", "调用工具"):
                        lines.append(f"[AI] {content}")
                elif content:
                    lines.append(f"[AI] {content}")
            elif mtype == "ToolMessage":
                if content:
                    lines.append(f"[工具结果] {content[:1000]}")
            elif mtype == "HumanMessage":
                # 保留任务信息（"任务名称/任务描述"等）；仅剔除纯 <current_time> 注入消息
                if content and not content.startswith("<current_time>"):
                    lines.append(f"[用户] {content[:500]}")
        return "\n".join(lines) or "(无历史消息)"

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

        prompt 来源：Langfuse ``general_evaluator_prompt``（文本类型，含
        ``{{history_messages}}`` / ``{{tools_desc}}`` 占位符），由 ``load_judge_prompt``
        渲染后整体作为一次对话输入（与基类单字符串 prompt 风格一致）。
        Langfuse 不可用 / prompt 不存在时跳过（返回空，不回落本地副本）。
        """
        if self._judge_llm is None:
            return {}, {}

        try:
            system_prompt = await self.load_judge_prompt(
                history_text=prompt_input.get("history_text", ""),
                tools_desc=prompt_input.get("tools_desc", ""),
            )
            if not system_prompt:
                logger.warning("General evaluation skipped: prompt 'general_evaluator_prompt' unavailable")
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

    async def load_judge_prompt(self, *, history_text: str, tools_desc: str) -> str | None:
        """获取评估系统提示词：唯一来源为 Langfuse ``general_evaluator_prompt``。

        prompt 必须预先在 Langfuse 创建（文本类型，占位符 ``{{history_messages}}`` /
        ``{{tools_desc}}``）。Langfuse 不可用 / prompt 不存在时返回 None（评估跳过）。
        本地副本 ``app/prompts/general_evaluator_prompt.md`` 仅作保存参考，不在此读取。
        """
        if self._langfuse is None:
            logger.warning("Langfuse client unavailable; cannot load prompt 'general_evaluator_prompt'")
            return None
        try:
            compiled = await asyncio.to_thread(
                lambda: self._langfuse.get_prompt("general_evaluator_prompt", type="text").compile(
                    history_messages=history_text,
                    tools_desc=tools_desc,
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
            logger.warning("Failed to load Langfuse prompt 'general_evaluator_prompt': %s", exc)
            return None


# ----------------------------------------------------------------------
# 执行节点评估触发（供 general_agent 调用）
# ----------------------------------------------------------------------


async def maybe_evaluate_general(
    *,
    trace_id: str,
    task_info: str,
    messages: list[Any],
    tools_desc: str,
    current_time: str,
    config: Any,
    runtime: Any,
) -> None:
    """按配置触发执行节点评估；未配置、被禁用、被采样或 Langfuse 不可用时静默跳过。

    与 ``plan_evaluator.maybe_evaluate_plan`` 保持一致的触发模式，但更轻量：
    直接从 config 读 ``general_evaluation`` 评估器设置，未配置则跳过（不引入旧配置兼容分支）。
    """
    try:
        from app.agents.evaluation.registry import create_evaluator
        from app.llm import create_llm_with_name

        context = runtime.context
        app_config = context.app_config

        eval_settings = app_config.get_evaluator("general_evaluation")
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
            "general_evaluation",
            app_config,
            llm_factory=_build_judge_llm,
            langfuse=context.langfuse_client,
        )
        if evaluator is None:
            return

        eval_input = GeneralEvaluationInput(
            task_info=task_info,
            history=_serialize_tool_messages(messages),
            tools_desc=tools_desc,
            current_time=current_time,
        )
        result = await evaluator.evaluate(
            trace_id=trace_id,
            prompt_input=evaluator.build_prompt_input(eval_input),
            messages=messages,
            config=config,
        )
        # 评估结果打点（page=evaluation，p0=评估器, p1=指标, p2=得分, p3=passed）
        if result is not None and result.scores:
            from app.core.tracking import TrackingPage, TrackingType
            from app.core.tracking.tracker import track

            role = eval_settings.model or ""
            for metric, score in result.scores.items():
                await track(
                    TrackingType.EVALUATION,
                    TrackingPage.EVALUATION,
                    model=role,
                    p0="GeneralEvaluator",
                    p1=metric,
                    p2=str(score),
                    p3=str(result.passed).lower(),
                )
    except Exception as exc:
        logger.warning("General evaluation skipped: %s", exc)
