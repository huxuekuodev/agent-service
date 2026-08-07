"""
评估器抽象基类。

设计目标：
  - 将「评估」抽象为可插拔的组件，一个评估器 = 一个 LLM-as-Judge 评估维度集。
  - 评估器通过 config.yaml 的 ``evaluators`` 列表注册（见 app/config/__init__.py），
    ``use`` 字段指向某个 BaseEvaluator 子类的 import 路径，工厂（registry.create_evaluator）
    负责实例化并注入 judge LLM。
  - 基类只负责通用编排：指标开关/阈值、LLM 打分 prompt 拼接、JSON 解析、Langfuse 落盘。

入参（__init__）：
  - judge_llm: 评估 LLM（由工厂按 evaluator.model 构建；None 时跳过 LLM 维度）。
  - system_prompt: 评估 LLM 的系统提示词（可定制）。
  - metrics: 指标定义，BaseModel 类型或 dict 均可（后期可传入不同指标集合）。
  - langfuse: Langfuse 客户端（可选）。

出参（evaluate 返回值 BaseEvaluationResult）：
  - scores: 各指标分数。
  - rationales: 各指标打分理由。
  - passed: 是否全部启用的指标通过（每个指标按自身 pass_score 判定）。

子类需实现：
  - build_prompt_input() -> dict：把评估输入转成 prompt 填充字段。
  - get_metrics() -> dict[str, MetricConfig]：指标定义（BaseModel 解析结构 + dict 自定义）。
  - build_judge_prompt(**prompt_input) -> str：拼接完整评估 prompt。
  - is_metric_applicable(name) -> bool：判断某指标在当前输入下是否可评（默认 True）。
  - （可选）rule_based_metric(name, prompt_input) -> MetricOutcome | None：规则校验优先，非 None 则不再走 LLM。
  - parse_llm_response(data: dict) -> dict[str, float]：解析 LLM JSON 输出（默认按指标名取值）。
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

__all__ = [
    "BaseEvaluationResult",
    "BaseEvaluator",
    "MetricConfig",
    "MetricOutcome",
]


@dataclass
class MetricConfig:
    """单个指标的配置。"""

    name: str
    """指标名（LLM 输出 JSON 中对应的键）。"""

    label: str = ""
    """指标显示名，空时用 name。"""

    enabled: bool = True
    """指标开关（config.yaml 的 metrics.<name>.enabled 可覆盖）。"""

    pass_score: float = 0.8
    """通过阈值：score >= pass_score 视为该指标通过。"""

    description: str = ""
    """指标说明（prompt 中的评分标准描述）。"""


@dataclass
class MetricOutcome:
    """单个指标的评估结果。"""

    score: float
    rationale: str = ""


@dataclass
class BaseEvaluationResult:
    """评估结果（出参契约）。"""

    scores: dict[str, float] = field(default_factory=dict)
    """各指标得分。"""

    rationales: dict[str, str] = field(default_factory=dict)
    """各指标打分理由。"""

    passed: bool = True
    """是否所有「已启用且适用」的指标均通过。"""

    def to_dict(self) -> dict:
        return {
            "scores": self.scores,
            "rationales": self.rationales,
            "passed": self.passed,
        }


TInput = TypeVar("TInput", bound=BaseModel, covariant=True)


def _serialize_message(msg: Any) -> dict:
    """序列化一条 LangChain 消息为可读 dict。"""
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        content = " ".join(parts)
    return {
        "type": type(msg).__name__,
        "content": str(content)[:2000],
        "name": getattr(msg, "name", None),
    }


class BaseEvaluator:
    """评估器抽象基类。"""

    #: 评估器显示名（config.yaml evaluators.display_name 可覆盖）。
    name: str = "base_evaluator"

    #: 默认指标定义。子类必须实现。值为 MetricConfig 或可转成 MetricConfig 的 dict。
    default_metrics: dict[str, MetricConfig | dict[str, Any]] = {}

    def __init__(
        self,
        *,
        judge_llm: Any | None = None,
        system_prompt: str | None = None,
        metrics: dict[str, Any] | None = None,
        langfuse: Any | None = None,
        trace_name: str | None = None,
        sample_rate: float = 1.0,
        name: str | None = None,
    ) -> None:
        """
        Args:
            judge_llm: 评估 LLM。None 时跳过所有 LLM 维度（规则维度仍会执行）。
            system_prompt: 评估 LLM 的系统提示词；None 时用类属性 ``default_system_prompt``。
            metrics: 指标定义覆盖。支持 dict[metric_name, dict]，可覆盖 label/enabled/pass_score/
                description；不在此处的指标保留类默认值。传 {name: {"enabled": false}} 可关闭单个指标。
            langfuse: Langfuse 客户端。None 时不写 observation/scores。
            trace_name: Langfuse observation 名称，默认 ``evaluation/{self.name}``。
            sample_rate: 采样率 0-1。
            name: 覆盖类属性 ``self.name``（config 的 display_name 通常传这里）。
        """
        self._judge_llm = judge_llm
        self._system_prompt = system_prompt or self.default_system_prompt
        self._langfuse = langfuse
        self._sample_rate = sample_rate
        if name:
            self.name = name
        self._trace_name = trace_name or f"evaluation/{self.name}"

        self._metrics: dict[str, MetricConfig] = self._build_metrics(metrics or {})

    # ------------------------------------------------------------------
    # 子类定制点
    # ------------------------------------------------------------------

    @property
    def default_system_prompt(self) -> str:
        """默认系统提示词（子类可覆盖）。"""
        return "你是评估器。对给定输入按评估维度打分，输出严格 JSON。"

    @property
    def default_judge_prompt(self) -> str:
        """默认的 judge prompt 模板（子类可覆盖）。"""
        return "你是评估器。根据输入内容，对以下指标打分。\n【评分标准】\n{metric_criteria}\n【输入】\n{prompt_input}\n【输出格式】严格 JSON，不要输出其他内容：\n{output_schema}"

    def get_metrics(self) -> dict[str, MetricConfig]:
        """返回合并后的指标定义（默认值 + 构造时覆盖）。子类可重写。"""
        return dict(self._metrics)

    def is_metric_applicable(self, name: str, prompt_input: dict[str, Any] | None = None) -> bool:
        """某指标在当前输入下是否可评。默认全部可评；无任务输出等场景子类可跳过。"""
        return True

    def build_prompt_input(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """把评估输入转换成 prompt 填充字段。子类必须实现。"""
        raise NotImplementedError

    def build_judge_prompt(self, *, system_prompt: str, metric_criteria: str, prompt_input: dict, output_schema: str) -> str:
        """拼接完整 judge prompt。子类可覆盖。"""
        return self.default_judge_prompt.format(
            metric_criteria=metric_criteria,
            prompt_input=json.dumps(prompt_input, ensure_ascii=False, indent=2),
            output_schema=output_schema,
        )

    def rule_based_metric(self, name: str, prompt_input: dict) -> MetricOutcome | None:
        """规则校验优先。返回非 None 时，该指标直接用此结果（不再走 LLM）。"""
        return None

    def parse_llm_response(self, data: dict) -> dict[str, float]:
        """从 LLM 输出 dict 中解析指标得分。子类可覆盖（处理额外键等）。"""
        scores: dict[str, float] = {}
        for name, cfg in self.get_metrics().items():
            if cfg.enabled and name in data:
                try:
                    scores[name] = float(data[name])
                except (TypeError, ValueError):
                    logger.warning("Cannot parse score for metric '%s': %r", name, data.get(name))
        return scores

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    async def evaluate(
        self,
        *,
        trace_id: str,
        prompt_input: dict[str, Any],
        messages: list[Any] | None = None,
        config: Any | None = None,
    ) -> BaseEvaluationResult | None:
        """执行评估，返回结果；被采样跳过时返回 None。

        Args:
            trace_id: 关联到现有 Langfuse trace。
            prompt_input: 评估输入（子类 build_prompt_input 的产出）。
            messages: 原始消息列表（用于补全 prompt_input 缺省字段）。
            config: RunnableConfig，用于 judge_llm 的 ainvoke。
        """
        if self._sample_rate < 1.0 and random.random() > self._sample_rate:
            return None

        if not prompt_input.get("history") and messages:
            prompt_input["history"] = [_serialize_message(m) for m in messages]

        result = BaseEvaluationResult()
        llm_scores: dict[str, float] = {}
        llm_rationales: dict[str, str] = {}

        # 规则优先，LLM 兜底
        for name, cfg in self.get_metrics().items():
            if not cfg.enabled or not self.is_metric_applicable(name, prompt_input):
                continue
            rule_outcome = self.rule_based_metric(name, prompt_input)
            if rule_outcome is not None:
                result.scores[name] = rule_outcome.score
                result.rationales[name] = rule_outcome.rationale
                continue
            if self._judge_llm is None:
                continue
            if name not in llm_scores:
                llm_scores, llm_rationales = await self._llm_judge(
                    trace_id=trace_id,
                    prompt_input=prompt_input,
                    config=config,
                )
            if name in llm_scores:
                result.scores[name] = llm_scores[name]
                result.rationales[name] = llm_rationales.get(name, "")

        # passed：所有「已启用且适用」的指标均达到自身 pass_score
        metrics = self.get_metrics()
        enabled_applicable = [name for name, cfg in metrics.items() if cfg.enabled and self.is_metric_applicable(name, prompt_input)]
        if enabled_applicable:
            result.passed = all(result.scores.get(name, 0.0) >= metrics[name].pass_score for name in enabled_applicable)
        else:
            result.passed = True

        self._emit_langfuse(trace_id=trace_id, prompt_input=prompt_input, result=result)
        return result

    # ------------------------------------------------------------------
    # LLM judge
    # ------------------------------------------------------------------

    def _build_metric_criteria(self) -> str:
        lines: list[str] = []
        for name, cfg in self.get_metrics().items():
            if not cfg.enabled:
                continue
            label = cfg.label or name
            lines.append(f"{name} ({label}): {cfg.description or '(无描述)'}。通过线: {cfg.pass_score}")
        return "\n".join(lines)

    def _build_output_schema(self) -> str:
        keys = [name for name, cfg in self.get_metrics().items() if cfg.enabled]
        return json.dumps({name: 0.0 for name in keys} | {"rationale": "简要说明"}, ensure_ascii=False)

    async def _llm_judge(
        self,
        *,
        trace_id: str,
        prompt_input: dict[str, Any],
        config: Any | None,
    ) -> tuple[dict[str, float], dict[str, str]]:
        if self._judge_llm is None:
            return {}, {}
        try:
            prompt = self.build_judge_prompt(
                system_prompt=self._system_prompt,
                metric_criteria=self._build_metric_criteria(),
                prompt_input=prompt_input,
                output_schema=self._build_output_schema(),
            )
            response = await self._judge_llm.ainvoke(prompt, config=config)
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
    # Langfuse 写入
    # ------------------------------------------------------------------

    def _emit_langfuse(
        self,
        *,
        trace_id: str,
        prompt_input: dict[str, Any],
        result: BaseEvaluationResult,
    ) -> None:
        if self._langfuse is None:
            return
        try:
            span = self._langfuse.start_observation(
                name=self._trace_name,
                as_type="span",
                trace_context={"trace_id": trace_id},
                input=prompt_input,
                output=result.to_dict(),
            )
            for metric, value in result.scores.items():
                comment = result.rationales.get(metric, "")
                span.score(
                    name=f"{self._trace_name}/{metric}",
                    value=float(value),
                    data_type="NUMERIC",
                    comment=comment,
                )
            span.end()
        except Exception as exc:
            logger.warning("Failed to emit Langfuse %s evaluation: %s", self._trace_name, exc)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _build_metrics(self, overrides: dict[str, Any]) -> dict[str, MetricConfig]:
        metrics: dict[str, MetricConfig] = {}
        for name, default in self.default_metrics.items():
            cfg = default if isinstance(default, MetricConfig) else MetricConfig(name=name, **default)
            metrics[name] = cfg
        # 覆盖：config.yaml 的 metrics.<name> 可改 enabled/pass_score/label/description
        for name, override in overrides.items():
            if name in metrics:
                merged = dict(metrics[name].__dict__)
                if isinstance(override, dict):
                    merged.update(override)
                merged["name"] = name
                metrics[name] = MetricConfig(**merged)
            else:
                if isinstance(override, dict):
                    metrics[name] = MetricConfig(name=name, **override)
                else:
                    metrics[name] = MetricConfig(name=name)
        return metrics
