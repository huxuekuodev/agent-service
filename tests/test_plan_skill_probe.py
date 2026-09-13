"""技能前置校验注入 / 技能表达方式单元测试（离线）。

技能已不再由 `PlanOutput.skills` 候选声明，改为**任务上的 `skill_id`** 表达；
只要计划里出现技能任务，系统就注入一个 `skill_probe` 前置校验任务。
"""

from __future__ import annotations

from app.agents.nodes.plan_model_node import (
    SKILL_PROBE_ID,
    PlanOutput,
    PlanTask,
    _inject_skill_probe,
    _skill_ids_of,
    _summarize_task_results,
)
from app.agents.subtask import SubTask


def test_plan_output_has_no_skills_field() -> None:
    """技能不再有独立声明字段（技能与任务是同一件事）。"""
    assert "skills" not in PlanOutput.model_fields
    assert list(PlanOutput.model_fields) == ["action", "title", "tasks", "answer"]


def test_plan_task_exposes_skill_id_without_step() -> None:
    assert "skill_id" in PlanTask.model_fields
    assert "sop_step" not in PlanTask.model_fields
    assert "sop_step" not in SubTask.model_fields


def test_skill_ids_collected_in_task_order_without_duplicates() -> None:
    tasks = [
        SubTask(plan_id="task1", skill_id="query-weather"),
        SubTask(plan_id="task2"),
        SubTask(plan_id="task3", skill_id="query-weather"),
        SubTask(plan_id="task4", skill_id="yuque-diff"),
    ]
    assert _skill_ids_of(tasks) == ["query-weather", "yuque-diff"]
    assert _skill_ids_of([SubTask(plan_id="task1")]) == []


def test_probe_injected_only_when_skill_task_exists() -> None:
    plain = [SubTask(plan_id="task1", name="普通任务")]
    assert _inject_skill_probe(plain) == plain

    tasks = [SubTask(plan_id="task1", name="查天气", skill_id="query-weather")]
    result = _inject_skill_probe(tasks)
    assert [t.plan_id for t in result] == [SKILL_PROBE_ID, "task1"]
    probe = result[0]
    assert probe.skill_id == "query-weather" and probe.deps == []
    assert probe.sort < result[1].sort
    assert result[1].deps == [SKILL_PROBE_ID]


def test_probe_not_duplicated_and_deps_preserved() -> None:
    tasks = [
        SubTask(plan_id="task1", name="A", skill_id="query-weather"),
        SubTask(plan_id="task2", name="B", skill_id="query-weather", deps=["task1"]),
    ]
    result = _inject_skill_probe(tasks)
    assert [t.plan_id for t in result].count(SKILL_PROBE_ID) == 1
    # 已有依赖保持原样，probe 前置
    assert result[2].deps == [SKILL_PROBE_ID, "task1"]
    # 技能不再被自动"补齐"到没有 skill_id 的任务上（由规划模型显式标注）
    assert all(t.skill_id for t in result[1:])


def test_probe_uses_first_skill_as_primary() -> None:
    tasks = [
        SubTask(plan_id="task1", name="A", skill_id="query-weather"),
        SubTask(plan_id="task2", name="B", skill_id="yuque-diff"),
    ]
    probe = _inject_skill_probe(tasks)[0]
    assert probe.skill_id == "query-weather"
    assert "yuque-diff" in probe.desc


# --------------------------------------------------------------------------- 空输出兜底


def test_summarize_results_ignores_internal_and_unfinished_tasks() -> None:
    """兜底汇总：排除系统内部任务（skill_probe）与未完成/空结果任务。"""
    tasks = [
        SubTask(plan_id=SKILL_PROBE_ID, result="技能「query-weather」校验：可用", step_statuses="completed"),
        SubTask(plan_id="task1", name="查天气", result="北京晴，29℃", step_statuses="completed"),
        SubTask(plan_id="task2", name="待执行", result="", step_statuses="not_started"),
        SubTask(plan_id="task3", name="进行中", result="半截结果", step_statuses="in_progress"),
    ]
    assert _summarize_task_results(tasks) == "北京晴，29℃"


def test_summarize_results_multiple_and_capped() -> None:
    tasks = [SubTask(plan_id=f"task{i}", name=f"任务{i}", result=f"结果{i}", step_statuses="completed") for i in range(1, 9)]
    summary = _summarize_task_results(tasks, max_items=3)
    assert "结果1" in summary and "结果3" in summary and "结果4" not in summary
    assert "另有 5 项未列出" in summary


def test_summarize_results_empty_when_nothing_useful() -> None:
    assert _summarize_task_results([]) == ""
    assert _summarize_task_results([SubTask(plan_id="task1", result="", step_statuses="completed")]) == ""
    assert _summarize_task_results([SubTask(plan_id=SKILL_PROBE_ID, result="内部校验文本", step_statuses="completed")]) == ""
