"""技能前置校验注入 / 技能表达方式单元测试（离线）。

技能已不再由 `PlanOutput.skills` 候选声明，改为**任务上的 `skill_id`** 表达；
只要计划里出现技能任务，系统就注入一个 `skill_probe` 前置校验任务。
"""

from __future__ import annotations

from app.agents.nodes.plan_model_node import SKILL_PROBE_ID, PlanOutput, PlanTask, _inject_skill_probe, _skill_ids_of
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
