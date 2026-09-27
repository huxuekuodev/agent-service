"""评估闭环单元测试（离线：策略判定 / 身份 / 记录器 / 指标适用性）。"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.evaluators.general_evaluator import GeneralEvaluator, _has_tool_activity
from app.evaluation import begin_run, current_meta, decide_trigger, new_run_id, record_evaluation, register_sink, update_meta
from app.evaluation.context import RunMeta

# --------------------------------------------------------------------------- 触发点判定


@pytest.fixture(autouse=True)
def _policy(monkeypatch: pytest.MonkeyPatch):
    """固定策略配置，避免依赖本机 config.yaml。"""
    from app.config import get_app_config

    policy = get_app_config().evaluation_policy
    monkeypatch.setattr(policy, "mode", "always", raising=False)
    monkeypatch.setattr(policy, "baseline_sample_rate", 0.0, raising=False)
    monkeypatch.setattr(policy, "triggers", {"plan_done": True, "task_done": True, "final_answer": True}, raising=False)
    monkeypatch.setattr(policy, "thresholds", {}, raising=False)
    return policy


def test_worth_signals_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.evaluation.policy import worth_signals

    assert worth_signals({"replan": True}) == ["replan"]
    assert worth_signals({"task_count": 3}) == ["many_tasks"]
    assert worth_signals({"task_count": 1}) == []


def test_worth_it_mode_skips_ordinary_turn_then_allows_signalled_one(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_app_config

    monkeypatch.setattr(get_app_config().evaluation_policy, "mode", "worth_it", raising=False)
    begin_run()
    assert decide_trigger("plan_done").should is False  # 无信号 → 普通轮次
    update_meta(signals={"clarify": True})
    assert decide_trigger("plan_done").should is True  # 命中信号 → 评


def test_mode_off_blocks_every_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_app_config

    monkeypatch.setattr(get_app_config().evaluation_policy, "mode", "off", raising=False)
    begin_run()
    for trigger in ("plan_done", "task_done", "final_answer"):
        assert decide_trigger(trigger).should is False


def test_chain_level_sampling_can_disable_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_app_config

    monkeypatch.setattr(get_app_config().evaluation_policy, "thresholds", {"sample_rate": 0.0}, raising=False)
    decision = begin_run()
    assert decision.enabled is False
    assert decide_trigger("plan_done").should is False


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


# --------------------------------------------------------------------------- 三个触发点与评估链


def test_begin_run_opens_chain_and_marks_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """调用 agent 那一层开链：run_id + eval_enabled + 触发点开关都写进 RunMeta。"""
    from app.config import get_app_config

    monkeypatch.setattr(get_app_config().evaluation_policy, "mode", "always", raising=False)
    decision = begin_run()
    meta = current_meta()
    assert decision.enabled is True
    assert meta.run_id == decision.run_id and len(meta.run_id) == 16
    assert meta.triggers == {"plan_done": True, "task_done": True, "final_answer": True}


def test_begin_run_reuses_given_run_id() -> None:
    """resume 复用链 id：同一用户回合的三个触发点属于同一条链。"""
    decision = begin_run(run_id="fixed-run-id")
    assert decision.run_id == "fixed-run-id"
    assert current_meta().run_id == "fixed-run-id"


def test_decide_trigger_respects_switches_and_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_app_config

    policy = get_app_config().evaluation_policy
    monkeypatch.setattr(policy, "mode", "always", raising=False)
    monkeypatch.setattr(policy, "triggers", {"plan_done": True, "task_done": False, "final_answer": True}, raising=False)
    begin_run()
    assert decide_trigger("plan_done").should is True
    assert decide_trigger("task_done").should is False  # 触发点关闭

    monkeypatch.setattr(policy, "triggers", {"plan_done": True, "task_done": True, "final_answer": True}, raising=False)
    monkeypatch.setattr(policy, "thresholds", {"min_task_count": 3}, raising=False)
    begin_run()
    assert decide_trigger("task_done", task_count=2).should is False  # 未达任务数阈值
    assert decide_trigger("task_done", task_count=3).should is True


def test_decide_trigger_blocked_when_run_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_app_config

    monkeypatch.setattr(get_app_config().evaluation_policy, "mode", "off", raising=False)
    begin_run()
    for trigger in ("plan_done", "task_done", "final_answer"):
        assert decide_trigger(trigger).should is False


def test_new_run_id_is_unique() -> None:
    assert new_run_id() != new_run_id()


def test_parse_llm_response_filters_unknown_metric_keys() -> None:
    """judge 常把 schema 里的 enabled/pass_score 一起回传，不能被当成指标（实测出现过）。"""
    from app.agents.evaluators.final_answer_evaluator import FinalAnswerEvaluator

    evaluator = FinalAnswerEvaluator()
    scores = evaluator.parse_llm_response({"question_fit": 4, "groundedness": 5, "completeness": 3, "enabled": True, "pass_score": 3.0})
    assert set(scores) == {"question_fit", "groundedness", "completeness"}
    assert scores["question_fit"] == 4.0


def test_final_answer_metrics_not_applicable_without_answer() -> None:
    from app.agents.evaluators.final_answer_evaluator import FinalAnswerEvaluator

    evaluator = FinalAnswerEvaluator()
    assert evaluator.is_metric_applicable("question_fit", {"final_answer": "(空)"}) is False
    assert evaluator.is_metric_applicable("question_fit", {"final_answer": "有答复"}) is True
