"""中断/恢复协议与计划合并语义单元测试（离线）。"""

from __future__ import annotations

import json

from app.agents.interrupts import DEFAULT_QUESTION_ID, AskAnswer, build_ask, extract_interrupt, parse_answer, question_of
from app.agents.subtask import SubTask
from app.agents.thread_state import merge_plan_tasks

# --------------------------------------------------------------------------- 协议：问什么


def test_build_ask_shape_is_multi_question_ready() -> None:
    payload = build_ask(
        kind="plan_review",
        title="计划已生成",
        items=[{"plan_id": "task1", "name": "查天气"}],
        questions=[{"id": DEFAULT_QUESTION_ID, "question": "需要调整吗？", "options": [], "allow_custom": True}],
    )
    assert payload["type"] == "plan_review"
    assert payload["items"][0]["plan_id"] == "task1"
    assert question_of(payload)["allow_custom"] is True
    # 无问题时是纯展示卡片（允许留言）
    assert question_of(build_ask(kind="notice")) is None


# --------------------------------------------------------------------------- 协议：怎么答


def test_parse_answer_new_protocol_continue() -> None:
    answer = parse_answer({"interrupt_id": "i1", "answers": []})
    assert isinstance(answer, AskAnswer)
    assert answer.answered is True and answer.continue_ is True and answer.feedback == ""


def test_parse_answer_new_protocol_with_feedback() -> None:
    answer = parse_answer({"answers": [{"id": DEFAULT_QUESTION_ID, "selected": [], "custom": " 把上海换成北京 "}]})
    assert answer.continue_ is False
    assert answer.feedback == "把上海换成北京"


def test_parse_answer_new_protocol_multiple_questions() -> None:
    answer = parse_answer(
        {
            "answers": [
                {"id": "q1", "selected": ["删除"], "custom": ""},
                {"id": "q2", "selected": [], "custom": "另外保留日志"},
            ]
        }
    )
    assert answer.selected == ["删除"]
    assert answer.feedback == "另外保留日志"
    assert answer.continue_ is False


def test_parse_answer_legacy_forms() -> None:
    """兼容旧协议（bool + suggestion）与其 JSON 字符串形态。"""
    legacy = parse_answer({"continue": False, "suggestion": "换个城市"})
    assert legacy.continue_ is False and legacy.feedback == "换个城市"

    as_string = parse_answer(json.dumps({"continue": True}))
    assert as_string.continue_ is True and as_string.feedback == ""

    plain = parse_answer("直接回复纯文本")
    assert plain.feedback == "直接回复纯文本" and plain.continue_ is False


def test_parse_answer_unanswered_on_garbage() -> None:
    assert parse_answer(None).answered is False
    assert parse_answer(123).answered is False


def test_extract_interrupt_from_updates_chunk() -> None:
    class _Intr:
        def __init__(self, value: str) -> None:
            self.id = "abc"
            self.value = value

    chunk = {"type": "updates", "data": {"__interrupt__": (_Intr(json.dumps({"type": "plan_review", "items": []})),)}}
    found = extract_interrupt(chunk)
    assert found is not None
    interrupt_id, payload = found
    assert interrupt_id == "abc" and payload["type"] == "plan_review"

    # 非 dict 的中断值兜底成 text，便于前端至少能展示
    found2 = extract_interrupt({"type": "updates", "data": {"__interrupt__": (_Intr("不是JSON"),)}})
    assert found2 is not None and found2[1] == {"text": "不是JSON"}

    assert extract_interrupt({"type": "custom", "data": {}}) is None
    assert extract_interrupt({"type": "updates", "data": {"plan_model_node": {}}}) is None


# --------------------------------------------------------------------------- 计划合并语义


def test_not_started_task_definition_can_be_revised() -> None:
    """未开始的任务：重排计划时 name/desc/deps/skill_id 可以改（修 bug 的核心）。"""
    current = [SubTask(plan_id="task1", name="查询上海天气", desc="查上海", skill_id="query-weather")]
    update = [SubTask(plan_id="task1", name="查询北京天气", desc="查北京", skill_id="query-weather")]
    merged = merge_plan_tasks(current, update)
    assert merged[0].name == "查询北京天气"
    assert merged[0].desc == "查北京"


def test_started_task_definition_is_frozen() -> None:
    """已开始/已完成的任务：定义冻结，只更新状态字段（结果属于旧定义）。"""
    current = [SubTask(plan_id="task1", name="查询上海天气", desc="查上海", step_statuses="completed", result="旧结果")]
    update = [SubTask(plan_id="task1", name="查询北京天气", desc="查北京", step_statuses="completed")]
    merged = merge_plan_tasks(current, update)
    assert merged[0].name == "查询上海天气"
    assert merged[0].desc == "查上海"
    assert merged[0].result == "旧结果"

    running = merge_plan_tasks(
        [SubTask(plan_id="t2", name="进行中", step_statuses="in_progress")],
        [SubTask(plan_id="t2", name="改名", step_statuses="in_progress")],
    )
    assert running[0].name == "进行中"


def test_default_values_never_wipe_existing_state() -> None:
    """只带状态的默认 SubTask（result="" / step_statuses=not_started）不能清空已有内容。"""
    current = [SubTask(plan_id="task1", name="A", result="已查到的数据", step_statuses="completed")]
    merged = merge_plan_tasks(current, [SubTask(plan_id="task1", step_statuses="in_progress")])
    assert merged[0].result == "已查到的数据", "空 result 不应清掉已有结果"
    assert merged[0].step_statuses == "in_progress"

    # in_progress 的任务不会被默认的 not_started 重置
    merged2 = merge_plan_tasks([SubTask(plan_id="t", step_statuses="in_progress")], [SubTask(plan_id="t")])
    assert merged2[0].step_statuses == "in_progress"


def test_new_tasks_are_appended_and_state_fields_always_update() -> None:
    current = [SubTask(plan_id="task1", name="A", step_statuses="not_started")]
    update = [
        SubTask(plan_id="task1", step_statuses="in_progress"),
        SubTask(plan_id="task2", name="B"),
    ]
    merged = merge_plan_tasks(current, update)
    assert {t.plan_id for t in merged} == {"task1", "task2"}
    assert next(t for t in merged if t.plan_id == "task1").step_statuses == "in_progress"
