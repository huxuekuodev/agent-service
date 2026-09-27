"""评估策略：**在调用 agent 的那一层**一次性定阈值，节点内部不再自己判断。

为什么这样切：评估有三个触发点（规划完成 / 每个任务完成 / 最终回复），它们属于**同一次用户回合**。
若每个节点各自决定"要不要评"，就会出现半条链（只评了规划、没评最终回复），
既看不出"整轮质量"，也无法把三次评估关联起来。因此：

  - ``begin_run()``：**调用 agent 时**（AgentService.stream 入口）一次性决定
    本次回合是否纳入评估、哪些触发点开、以及各类阈值，写进 RunMeta（含 run_id，形成一条评估链）；
  - ``decide_trigger()``：触发点处的**唯一判定点**（在真正调用 judge agent 之前调用），
    按链级开关 + 阈值（如"任务数 ≥ N 才评任务"）+ 采样率决定这一次是否真的评。

配置（``evaluation_policy``）::

    mode: always            # always（测试阶段全触发）/ worth_it（只评有信号的轮次）/ off
    triggers:               # 三个触发点开关
      plan_done: true
      task_done: true
      final_answer: true
    thresholds:             # 阈值（调用 agent 时生效）
      min_task_count: 0         # 任务数达到该值才评任务完成（0 = 全部）
      max_evals_per_run: 12     # 单条链最多评估次数（防长链刷爆 judge）
      sample_rate: 1.0          # 链级采样率
    async_enabled: true     # 全部异步（不占对话延迟）
    max_concurrency: 2
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "MANY_TASKS_THRESHOLD",
    "RunDecision",
    "TriggerDecision",
    "begin_run",
    "decide_trigger",
    "policy_config",
]

#: "值得评"信号里，任务数达到该值即视为复杂计划
MANY_TASKS_THRESHOLD = 3

#: 三个触发点
TRIGGERS = ("plan_done", "task_done", "final_answer")

#: "值得评"信号 → 说明（``mode=worth_it`` 时至少命中一个才评，控制 judge 成本）
WORTH_SIGNALS = (
    ("replan", "在已有计划上继续（update/审查轮）"),
    ("failed_task", "有任务执行失败"),
    ("clarify", "本次请求了澄清"),
    ("first_turn", "会话首轮"),
    ("user_follow_up", "用户追问"),
    ("many_tasks", "任务数较多"),
    ("skill_used", "用到技能"),
)


def worth_signals(signals: dict[str, Any] | None = None) -> list[str]:
    """命中的"值得评"信号列表（供 worth_it 模式与事后统计使用）。"""
    signals = dict(signals or {})
    hit: list[str] = []
    for name, _ in WORTH_SIGNALS:
        if name == "many_tasks":
            if int(signals.get("task_count") or 0) >= MANY_TASKS_THRESHOLD:
                hit.append(name)
        elif signals.get(name):
            hit.append(name)
    return hit


@dataclass(frozen=True)
class RunDecision:
    """链级判定结果（调用 agent 时产生一次）。"""

    enabled: bool
    reason: str = ""
    run_id: str = ""
    triggers: dict[str, bool] = field(default_factory=dict)
    thresholds: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TriggerDecision:
    """触发点判定结果。"""

    should: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.should


def policy_config() -> Any:
    """取评估策略配置（config.yaml evaluation_policy）。"""
    from app.config import get_app_config

    return get_app_config().evaluation_policy


def begin_run(run_id: str | None = None) -> RunDecision:
    """**调用 agent 时**决定本次回合的评估策略，并写入 RunMeta（开一条评估链）。

    这一步只做"链级"决策，不依赖节点内部状态；节点随后无条件调用各自的评估入口，
    由 :func:`decide_trigger` 按这里定下的开关与阈值逐次放行。

    Args:
        run_id: 复用已有链 id（``/resume`` 继续同一次用户回合时传入，保证链条不断）。
    """
    from app.evaluation.context import current_meta, new_run_id, update_meta

    config = policy_config()
    thresholds = dict(getattr(config, "thresholds", {}) or {})
    requested = dict(getattr(config, "triggers", {}) or {})
    triggers = {name: bool(requested.get(name, True)) for name in TRIGGERS}

    mode = str(getattr(config, "mode", "always") or "always").lower()
    if mode == "off":
        decision = RunDecision(False, "evaluation_policy.mode=off", current_meta().run_id, triggers, thresholds)
    else:
        sample_rate = float(thresholds.get("sample_rate", 1.0) or 0.0)
        enabled = not (sample_rate < 1.0 and random.random() > sample_rate)
        reason = "mode=always（测试阶段全触发）" if mode == "always" else f"mode={mode}"
        if not enabled:
            reason = f"链级采样未命中（sample_rate={sample_rate}）"
        decision = RunDecision(enabled, reason, run_id or current_meta().run_id or new_run_id(), triggers, thresholds)

    update_meta(
        run_id=decision.run_id,
        eval_enabled=decision.enabled,
        eval_reason=decision.reason,
        triggers=decision.triggers,
        signals={"eval_run_reason": decision.reason},
    )
    return decision


def decide_trigger(trigger: str, *, task_index: int = 0, task_count: int = 0) -> TriggerDecision:
    """触发点处的**唯一判定点**（调用 judge agent 之前调用）。

    Args:
        trigger: ``plan_done`` / ``task_done`` / ``final_answer``。
        task_index: 第几个任务（``task_done`` 用；从 0 起）。
        task_count: 本次计划的任务总数（阈值判断用）。

    Returns:
        :class:`TriggerDecision`
    """
    from app.evaluation.context import current_meta

    meta = current_meta()
    if not meta.eval_enabled:
        return TriggerDecision(False, meta.eval_reason or "本次回合未纳入评估")
    if not bool(meta.triggers.get(trigger, True)):
        return TriggerDecision(False, f"触发点 {trigger} 已关闭（evaluation_policy.triggers.{trigger}=false）")

    config = policy_config()
    mode = str(getattr(config, "mode", "always") or "always").lower()
    if mode == "worth_it":
        # 成本控制：只在"出过问题/信息量大"的轮次评（信号由节点在触发前写入 RunMeta）
        hit = worth_signals(meta.signals)
        if not hit:
            baseline = float(getattr(config, "baseline_sample_rate", 0.0) or 0.0)
            if baseline <= 0 or random.random() > baseline:
                return TriggerDecision(False, "普通轮次（未命中值得评信号）")

    thresholds = dict(getattr(config, "thresholds", {}) or {})
    max_total = int(thresholds.get("max_evals_per_run", 0) or 0)
    if max_total and evals_in_run(meta.run_id) >= max_total:
        return TriggerDecision(False, f"单链评估次数达到上限 {max_total}")
    if trigger == "task_done":
        min_tasks = int(thresholds.get("min_task_count", 0) or 0)
        if min_tasks and task_count < min_tasks:
            return TriggerDecision(False, f"任务数 {task_count} < 阈值 {min_tasks}")
        max_tasks = int(thresholds.get("max_task_evals", 0) or 0)
        if max_tasks and task_index >= max_tasks:
            return TriggerDecision(False, f"任务评估次数达到上限 {max_tasks}")
    return TriggerDecision(True, f"{trigger} 放行")


#: 单链已完成的评估次数（进程内计数；用于 ``max_evals_per_run`` 上限，防长链刷爆 judge）
_run_eval_counts: dict[str, int] = {}


def note_eval(run_id: str) -> None:
    """记录一条链又完成了一次评估。"""
    if run_id:
        _run_eval_counts[run_id] = _run_eval_counts.get(run_id, 0) + 1


def evals_in_run(run_id: str) -> int:
    """该链已完成的评估次数。"""
    return _run_eval_counts.get(run_id, 0) if run_id else 0


def reset_run_counters(run_id: str = "") -> None:
    """清空链计数器（跑批/测试用）。"""
    if run_id:
        _run_eval_counts.pop(run_id, None)
    else:
        _run_eval_counts.clear()
