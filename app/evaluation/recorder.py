"""评估记录器：统一出口（落库 + 打点 + 后台异步 + 低分样本归档）。

四件事一次做完，避免各调用点各写一份：

1. **落库**（``evaluations``）：每条指标一行，带完整身份（会话/消息/任务/技能/模型/prompt 版本/代码版本）；
2. **打点**（``page=evaluation``）：p0 评估器、p1 指标、p2 得分、p3 passed、**p4 会话、p5 计划或任务、p6 prompt 版本、p7 被评模型**；
3. **低分归档**（``eval_samples``）：低于阈值时把"问题 + 产出 + 评语 + 上下文"整份存下来，
   这是后续改 prompt / 改技能 / 构建离线评测集的原料；
4. **后台执行**：judge 与写库都不拖慢对话（原先节点要 await 完评估才返回）。

另有 ``register_sink()``：离线跑批/单测可挂收集器，直接拿到评估记录而不依赖数据库。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from app.core.log import logger
from app.evaluation.context import current_meta

__all__ = ["BAD_CASE_SCORE", "aclose_recorder", "record_evaluation", "register_sink", "submit_background"]

#: 低分归档阈值（低于它的指标会连同完整上下文存进 eval_samples）
BAD_CASE_SCORE = 3.0

_tasks: set[asyncio.Task] = set()
_semaphore: asyncio.Semaphore | None = None
_sinks: list[Callable[[dict[str, Any]], None]] = []


def register_sink(sink: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
    """注册记录收集器（离线跑批/单测用），返回取消注册的函数。"""
    _sinks.append(sink)

    def _unregister() -> None:
        if sink in _sinks:
            _sinks.remove(sink)

    return _unregister


def _policy() -> Any:
    from app.config import get_app_config

    return get_app_config().evaluation_policy


def _limit() -> asyncio.Semaphore:
    """并发限流（judge 与写库共用），上限取 config ``evaluation_policy.max_concurrency``。"""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max(1, int(getattr(_policy(), "max_concurrency", 2) or 2)))
    return _semaphore


def submit_background(coro: Any, *, what: str) -> None:
    """把协程丢到后台执行（不阻塞调用方）；异常只记日志，绝不影响对话。"""

    async def _runner() -> None:
        async with _limit():
            try:
                await coro
            except Exception as exc:
                logger.warning("[evaluation] 后台任务失败（{}）: {}", what, exc)

    task = asyncio.create_task(_runner())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def record_evaluation(
    *,
    evaluator: str,
    node: str,
    trigger: str = "",
    metric_scores: dict[str, float],
    rationales: dict[str, str] | None = None,
    passed: bool | None = None,
    judge_model: str = "",
    trace_id: str = "",
    prompt_input: dict[str, Any] | None = None,
    output_payload: dict[str, Any] | None = None,
    extra_meta: dict[str, Any] | None = None,
    background: bool = True,
) -> None:
    """记录一次评估结果（落库 + 打点 + 低分归档）。

    Args:
        evaluator: 评估器名（PlanEvaluator / GeneralEvaluator）。
        node: 被评节点（plan_node / general_agent）。
        metric_scores: 指标 → 分数。
        rationales: 指标 → 评语。
        passed: 整体是否通过（逐指标写入同一值）。
        judge_model: judge 用的模型角色。
        trace_id: Langfuse trace（便于回到完整调用链）。
        prompt_input: 评估输入（低分归档时存原文，可回放）。
        output_payload: 被评产出（计划/答复）。
        extra_meta: 追加进 signals 的字段（如 replan / failed_task 等运行信号）。
        background: 是否后台执行（``evaluation_policy.async_enabled=false`` 时强制同步）。
    """
    if not metric_scores:
        return
    meta = current_meta()
    if extra_meta:
        meta = meta.merged(signals={**meta.signals, **extra_meta})

    coro = _record_now(
        evaluator=evaluator,
        node=node,
        trigger=trigger,
        metric_scores=metric_scores,
        rationales=rationales or {},
        passed=passed,
        judge_model=judge_model,
        trace_id=trace_id,
        prompt_input=prompt_input or {},
        output_payload=output_payload or {},
        meta=meta,
    )
    if background and bool(getattr(_policy(), "async_enabled", True)):
        submit_background(coro, what=f"{evaluator}@{node or meta.node}")
        return
    await coro


async def _record_now(
    *,
    evaluator: str,
    node: str,
    trigger: str = "",
    metric_scores: dict[str, float],
    rationales: dict[str, str],
    passed: bool | None,
    judge_model: str,
    trace_id: str,
    prompt_input: dict[str, Any],
    output_payload: dict[str, Any],
    meta: Any,
) -> None:
    """落库 + 打点 + 低分归档（真正的副作用都集中在这里）。"""
    resolved_judge = judge_model or meta.judge_model
    resolved_node = node or meta.node
    rows = [
        {
            "trace_id": trace_id,
            "session_id": meta.session_id,
            "message_id": meta.message_id,
            "node": resolved_node,
            "evaluator": evaluator,
            "trigger": trigger,
            "run_id": meta.run_id,
            "metric": metric,
            "score": float(score),
            "passed": passed,
            "rationale": rationales.get(metric, ""),
            "plan_id": meta.plan_id,
            "task_id": meta.task_id,
            "skill_id": meta.skill_id,
            "run_model": meta.run_model,
            "run_prompt_version": meta.run_prompt_version,
            "judge_model": resolved_judge,
            "judge_prompt_version": meta.judge_prompt_version,
            "git_sha": meta.git_sha,
            "channel": meta.channel,
            "meta": {"signals": meta.signals},
        }
        for metric, score in metric_scores.items()
    ]

    # 0) 进程内收集器（离线跑批/单测）
    for sink in list(_sinks):
        try:
            sink(
                {
                    "evaluator": evaluator,
                    "node": resolved_node,
                    "trigger": trigger,
                    "run_id": meta.run_id,
                    "metric_scores": dict(metric_scores),
                    "rationales": dict(rationales),
                    "passed": passed,
                    "judge_model": resolved_judge,
                    "trace_id": trace_id,
                    "session_id": meta.session_id,
                    "plan_id": meta.plan_id,
                    "task_id": meta.task_id,
                    "skill_id": meta.skill_id,
                    "run_model": meta.run_model,
                    "run_prompt_version": meta.run_prompt_version,
                    "git_sha": meta.git_sha,
                    "channel": meta.channel,
                    "signals": dict(meta.signals),
                }
            )
        except Exception as exc:
            logger.warning("[evaluation] sink 处理失败: {}", exc)

    # 0.5) 链级计数（阈值 max_evals_per_run 用）
    try:
        from app.evaluation.policy import note_eval

        note_eval(meta.run_id)
    except Exception:  # 计数失败不影响记录
        pass

    # 1) 打点：补上身份与版本（p4 会话 / p5 计划或任务 / p6 prompt 版本 / p7 被评模型）
    try:
        from app.core.tracking import TrackingPage, TrackingType
        from app.core.tracking.tracker import track

        for metric, score in metric_scores.items():
            await track(
                TrackingType.EVALUATION,
                TrackingPage.EVALUATION,
                model=resolved_judge or meta.run_model,
                p0=evaluator,
                p1=metric,
                p2=str(score),
                p3=str(bool(passed)).lower(),
                p4=meta.session_id,
                p5=meta.plan_id or meta.task_id,
                p6=meta.run_prompt_version,
                p7=meta.run_model,
                p8=trigger,
                p9=meta.run_id,
            )
    except Exception as exc:
        logger.warning("[evaluation] 打点失败: {}", exc)

    # 2) 落库（业务库未配置时跳过；失败不影响对话）
    if not await _persist_rows(rows):
        return

    # 3) 低分归档（改进原料）
    try:
        await _archive_bad_cases(
            evaluator=evaluator,
            node=resolved_node,
            metric_scores=metric_scores,
            rationales=rationales,
            prompt_input=prompt_input,
            output_payload=output_payload,
            meta=meta,
            trace_id=trace_id,
        )
    except Exception as exc:
        logger.warning("[evaluation] 低分样本归档失败: {}", exc)


async def _persist_rows(rows: list[dict[str, Any]]) -> bool:
    """写入 evaluations；业务库不可用或失败时返回 False。"""
    try:
        from app.evaluation import store as eval_store
        from app.session import store as session_store

        if not session_store.is_available():
            return False
        await eval_store.insert_evaluations(rows)
        return True
    except Exception as exc:
        logger.warning("[evaluation] 评估落库失败: {}", exc)
        return False


async def _archive_bad_cases(
    *,
    evaluator: str,
    node: str,
    metric_scores: dict[str, float],
    rationales: dict[str, str],
    prompt_input: dict[str, Any],
    output_payload: dict[str, Any],
    meta: Any,
    trace_id: str,
) -> None:
    """把低于阈值的指标连同完整上下文归档为一条 bad case（含全部低分指标）。"""
    from app.evaluation import store as eval_store

    low = {metric: float(score) for metric, score in metric_scores.items() if float(score) < BAD_CASE_SCORE}
    if not low:
        return
    label = ",".join(sorted(low))
    await eval_store.insert_sample(
        kind="bad_case",
        label=label,
        metric=label,
        evaluator=evaluator,
        score=min(low.values()),
        session_id=meta.session_id,
        message_id=meta.message_id,
        trace_id=trace_id,
        input_payload=prompt_input,
        output_payload=output_payload,
        context={
            "node": node,
            "scores": {k: float(v) for k, v in metric_scores.items()},
            "rationales": rationales,
            "run_model": meta.run_model,
            "run_prompt_version": meta.run_prompt_version,
            "git_sha": meta.git_sha,
            "skill_id": meta.skill_id,
            "plan_id": meta.plan_id,
            "task_id": meta.task_id,
            "signals": meta.signals,
        },
    )
    logger.info("[evaluation] 已归档低分样本: {} {} 最低分={}", evaluator, label, min(low.values()))


async def aclose_recorder(*, timeout: float = 10.0) -> int:
    """等待后台评估任务收尾（应用关闭时调用），返回被强制取消的任务数。"""
    if not _tasks:
        return 0
    pending = list(_tasks)
    logger.info("[evaluation] 等待 {} 个后台评估任务收尾…", len(pending))
    _, still = await asyncio.wait(pending, timeout=timeout)
    for task in still:
        task.cancel()
    return len(still)
