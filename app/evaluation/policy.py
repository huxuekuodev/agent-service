"""评估策略："值得评才评"，把 judge 成本花在有信息量的轮次上。

背景：原先每次请求、每个节点、每个指标都跑 judge（``sample_rate=1.0``），成本高且信息量低——
大量"一次拆解就成功"的轮次评分集中在 4-5 分，看不出差异。真正有信号的是**出过问题的轮次**。

策略（``evaluation_policy.mode``）：

  - ``worth_it``（默认）：命中任一"值得评"信号才评，其余按 ``baseline_sample_rate`` 抽样；
  - ``always``：全量评（旧行为，调试/跑批用）；
  - ``off``：不评（跑批时可临时关掉在线评估，避免污染数据）。

"值得评"信号（任一命中即评）：

  - ``replan``            规划节点在已有计划上继续（update/审查轮）——最容易出现"没拆干净/依赖错"；
  - ``failed_task``       有任务执行失败；
  - ``clarify``           这次请求了澄清（判断"该不该澄清"最有价值）；
  - ``first_turn``        会话首轮（决定整段对话质量）；
  - ``user_follow_up``    用户连续追问（说明上一轮没答到点上）；
  - ``many_tasks``        任务数 ≥ 阈值（复杂计划的原子性/依赖最容易出问题）；
  - ``skill_used``        用到技能（技能维度改进的依据）。

判定结果一并返回原因，写入评估记录（``meta.signals``），便于事后统计"哪种轮次最容易出问题"。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

__all__ = ["EvaluationDecision", "decide", "policy_config"]

#: 任务数达到该值即视为"复杂计划"
MANY_TASKS_THRESHOLD = 3


@dataclass(frozen=True)
class EvaluationDecision:
    """是否评估的判定结果。"""

    should: bool
    reason: str = ""
    signals: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:  # 便于 `if decide(...):`
        return self.should


def policy_config() -> Any:
    """取评估策略配置（config.yaml evaluation_policy）。"""
    from app.config import get_app_config

    return get_app_config().evaluation_policy


def decide(*, signals: dict[str, Any] | None = None, sample_rate: float | None = None) -> EvaluationDecision:
    """判定本次是否值得评估。

    Args:
        signals: 运行信号（见模块 docstring），缺省键按 False 处理。
        sample_rate: 评估器自身的采样率（``evaluators[].sample_rate``）；策略放行后再按它抽样。

    Returns:
        :class:`EvaluationDecision`
    """
    config = policy_config()
    mode = str(getattr(config, "mode", "worth_it") or "worth_it").lower()
    if mode == "off":
        return EvaluationDecision(False, "策略关闭在线评估（evaluation_policy.mode=off）")

    signals = dict(signals or {})
    hit = _worth_it_signals(signals)

    if mode == "always":
        return EvaluationDecision(_sample(sample_rate), "mode=always", hit)
    if mode == "worth_it":
        if hit:
            return EvaluationDecision(_sample(sample_rate), f"命中信号: {','.join(hit)}", hit)
        baseline = float(getattr(config, "baseline_sample_rate", 0.0) or 0.0)
        if baseline > 0 and random.random() <= baseline:
            return EvaluationDecision(_sample(sample_rate), f"普通轮次抽样（{baseline}）", hit)
        return EvaluationDecision(False, "普通轮次（未命中值得评信号）", hit)
    # 未知模式按值得评处理，但记清楚
    return EvaluationDecision(_sample(sample_rate), f"未知 mode={mode}，按命中信号评估", hit)


def _worth_it_signals(signals: dict[str, Any]) -> list[str]:
    """命中的"值得评"信号列表。"""
    hit: list[str] = []
    if signals.get("replan"):
        hit.append("replan")
    if signals.get("failed_task"):
        hit.append("failed_task")
    if signals.get("clarify"):
        hit.append("clarify")
    if signals.get("first_turn"):
        hit.append("first_turn")
    if signals.get("user_follow_up"):
        hit.append("user_follow_up")
    if int(signals.get("task_count") or 0) >= MANY_TASKS_THRESHOLD:
        hit.append("many_tasks")
    if signals.get("skill_used"):
        hit.append("skill_used")
    return hit


def _sample(sample_rate: float | None) -> bool:
    """评估器采样率（1.0 / None 表示全采）。"""
    if sample_rate is None:
        return True
    return not (sample_rate < 1.0 and random.random() > sample_rate)
