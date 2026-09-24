"""评估闭环单元测试（离线：策略判定 / 身份 / 记录器 / 指标适用性）。"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.evaluation.general_evaluator import GeneralEvaluator, _has_tool_activity
from app.evaluation import current_meta, record_evaluation, register_sink, update_meta
from app.evaluation.context import RunMeta
from app.evaluation.policy import decide

# --------------------------------------------------------------------------- 策略判定


@pytest.fixture(autouse=True)
def _policy(monkeypatch: pytest.MonkeyPatch):
    """固定策略配置，避免依赖本机 config.yaml。"""
    from app.config import get_app_config

    policy = get_app_config().evaluation_policy
    monkeypatch.setattr(policy, "mode", "worth_it", raising=False)
    monkeypatch.setattr(policy, "baseline_sample_rate", 0.0, raising=False)
    return policy


def test_worth_it_signals_trigger_evaluation() -> None:
    for signals in (
        {"replan": True},
        {"failed_task": True},
        {"clarify": True},
        {"first_turn": True},
        {"user_follow_up": True},
        {"task_count": 3},
        {"skill_used": True},
    ):
        decision = decide(signals=signals)
        assert decision.should, f"信号未触发评估: {signals}"
        assert decision.signals


def test_ordinary_turn_is_skipped_by_default() -> None:
    decision = decide(signals={"task_count": 1})
    assert decision.should is False
    assert "普通轮次" in decision.reason


def test_mode_always_and_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_app_config

    policy = get_app_config().evaluation_policy
    monkeypatch.setattr(policy, "mode", "always", raising=False)
    assert decide(signals={}).should is True

    monkeypatch.setattr(policy, "mode", "off", raising=False)
    assert decide(signals={"clarify": True}).should is False


def test_evaluator_sample_rate_applies_after_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_app_config

    monkeypatch.setattr(get_app_config().evaluation_policy, "mode", "always", raising=False)
    assert decide(signals={}, sample_rate=0.0).should is False  # 采样率 0 → 不评


def test_baseline_sampling_can_let_ordinary_turns_through(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_app_config

    monkeypatch.setattr(get_app_config().evaluation_policy, "baseline_sample_rate", 1.0, raising=False)
    decision = decide(signals={})
    assert decision.should is True
    assert "抽样" in decision.reason


# --------------------------------------------------------------------------- 身份


def test_run_meta_carries_identity_and_resets() -> None:
    token = update_meta(session_id="s1", node="plan_node", run_model="default", run_prompt_version="langfuse:v4")
    try:
        meta = current_meta()
        assert meta.session_id == "s1"
        assert meta.node == "plan_node"
        assert meta.run_prompt_version == "langfuse:v4"
        assert meta.git_sha is not None  # 可能是空串（非 git 环境）
    finally:
        from app.evaluation import reset_meta

        reset_meta(token)
    assert current_meta().session_id == ""


def test_run_meta_defaults_are_safe() -> None:
    meta = RunMeta()
    assert meta.session_id == "" and meta.signals == {}
    assert meta.to_dict()["channel"] == "chat"


# --------------------------------------------------------------------------- 记录器


def test_record_evaluation_reaches_sink_with_identity() -> None:
    """记录器把身份/版本带给收集器（跑批与单测都靠它，不依赖数据库）。"""
    collected: list[dict] = []
    unregister = register_sink(collected.append)
    token = update_meta(session_id="sess-1", plan_id="task1", skill_id="query-weather", run_model="default", run_prompt_version="langfuse:v4")
    try:
        asyncio.run(
            record_evaluation(
                evaluator="PlanEvaluator",
                node="plan_node",
                metric_scores={"task_atomicity": 4.0, "dependency_correctness": 2.0},
                rationales={"task_atomicity": "ok", "dependency_correctness": "缺依赖"},
                passed=False,
                judge_model="evaluate_model",
                trace_id="trace-1",
                prompt_input={"user_messages": ["查天气"]},
                output_payload={"plan_action": "create"},
                background=False,
            )
        )
    finally:
        unregister()
        from app.evaluation import reset_meta

        reset_meta(token)

    assert len(collected) == 1
    record = collected[0]
    assert record["session_id"] == "sess-1"
    assert record["plan_id"] == "task1"
    assert record["skill_id"] == "query-weather"
    assert record["run_prompt_version"] == "langfuse:v4"
    assert record["metric_scores"] == {"task_atomicity": 4.0, "dependency_correctness": 2.0}
    assert record["passed"] is False


def test_record_evaluation_ignores_empty_scores() -> None:
    collected: list[dict] = []
    unregister = register_sink(collected.append)
    try:
        asyncio.run(record_evaluation(evaluator="X", node="plan_node", metric_scores={}, background=False))
    finally:
        unregister()
    assert collected == []


def test_sink_failure_does_not_break_recording(caplog: pytest.LogCaptureFixture) -> None:
    """收集器抛异常不能影响主流程（评估是旁路）。"""

    def bad_sink(_: dict) -> None:
        raise RuntimeError("boom")

    unregister = register_sink(bad_sink)
    try:
        asyncio.run(record_evaluation(evaluator="X", node="plan_node", metric_scores={"m": 3.0}, background=False))
    finally:
        unregister()


# --------------------------------------------------------------------------- 指标适用性


def test_path_efficiency_requires_tool_activity() -> None:
    """没有工具调用时"路径效率"不适用（否则纯回答任务被误判低效）。"""
    evaluator = GeneralEvaluator()
    assert evaluator.is_metric_applicable("path_efficiency", {"history": []}) is False
    assert evaluator.is_metric_applicable("path_efficiency", {"history": [{"type": "AIMessage", "content": "直接回答"}]}) is False
    assert evaluator.is_metric_applicable("path_efficiency", {"history": [{"type": "ToolMessage", "content": "结果"}]}) is True
    assert evaluator.is_metric_applicable("path_efficiency", {"history": [{"type": "AIMessage", "tool_calls": [{"name": "t"}]}]}) is True
    # 其他指标不受影响
    assert evaluator.is_metric_applicable("answer_cleanness", {"history": []}) is True


def test_has_tool_activity_tolerates_bad_shapes() -> None:
    assert _has_tool_activity([None, "x", {"type": ""}]) is False
    assert _has_tool_activity([{"type": "tool"}]) is True
