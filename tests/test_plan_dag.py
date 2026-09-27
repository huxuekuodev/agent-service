"""计划契约（DAG）单元测试：schema 不变式、拓扑校验/修复、就绪筛选、提交协议（离线）。"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import ValidationError

from app.agents.plan.dag import pick_ready, repair_topology, validate_topology
from app.agents.plan.protocol import SUBMIT_ACK, SUBMIT_TOOL_NAME, PlanSubmissionMiddleware, parse_submission, submit_plan
from app.agents.plan.schema import SKILL_PROBE_ID, PlanOutput
from app.agents.state.subtask import SubTask


def _task(plan_id: str, deps: list[str] | None = None, **kwargs) -> dict:
    return {"plan_id": plan_id, "name": plan_id, "desc": f"{plan_id} 的指令", "deps": deps or [], **kwargs}


# --------------------------------------------------------------------------- 契约不变式


def test_action_must_be_one_of_three() -> None:
    with pytest.raises(ValidationError):
        PlanOutput(action="answer", answer="x")


def test_complete_requires_empty_tasks_and_answer() -> None:
    assert PlanOutput(action="complete", answer="答案").tasks == []
    with pytest.raises(ValidationError):
        PlanOutput(action="complete", tasks=[_task("task1")], answer="答案")
    with pytest.raises(ValidationError):
        PlanOutput(action="complete", answer="   ")


def test_plan_actions_require_tasks_and_no_answer() -> None:
    with pytest.raises(ValidationError):
        PlanOutput(action="create")
    with pytest.raises(ValidationError):
        PlanOutput(action="create", tasks=[_task("task1")], answer="不该有答案")
    assert [t.plan_id for t in PlanOutput(action="update", tasks=[_task("task2")]).tasks] == ["task2"]


def test_plan_id_must_be_unique_and_safe() -> None:
    with pytest.raises(ValidationError) as err:
        PlanOutput(action="create", tasks=[_task("task1"), _task("task1")])
    assert "唯一" in str(err.value)

    with pytest.raises(ValidationError) as err:
        PlanOutput(action="create", tasks=[_task("task 1")])
    assert "plan_id" in str(err.value)


def test_reserved_probe_id_rejected() -> None:
    with pytest.raises(ValidationError) as err:
        PlanOutput(action="create", tasks=[_task(SKILL_PROBE_ID)])
    assert "保留" in str(err.value)


def test_empty_name_and_desc_rejected() -> None:
    with pytest.raises(ValidationError):
        PlanOutput(action="create", tasks=[_task("task1", name=" ")])
    with pytest.raises(ValidationError):
        PlanOutput(action="create", tasks=[_task("task1", desc="")])


def test_self_dependency_rejected() -> None:
    with pytest.raises(ValidationError) as err:
        PlanOutput(action="create", tasks=[_task("task1", deps=["task1"])])
    assert "依赖自己" in str(err.value)


def test_create_rejects_deps_outside_plan() -> None:
    """create 会整体替换旧计划，依赖不能指向本次没给出的任务。"""
    with pytest.raises(ValidationError) as err:
        PlanOutput(action="create", tasks=[_task("task2", deps=["task1"])])
    assert "update" in str(err.value)


def test_update_allows_deps_on_old_tasks() -> None:
    """update 是增补：允许依赖旧计划里已有的任务（校验交给 validate_topology）。"""
    plan = PlanOutput(action="update", tasks=[_task("task2", deps=["task1"])])
    assert plan.tasks[0].deps == ["task1"]


def test_cycle_rejected() -> None:
    with pytest.raises(ValidationError) as err:
        PlanOutput(action="create", tasks=[_task("task1", deps=["task2"]), _task("task2", deps=["task1"])])
    assert "循环依赖" in str(err.value)


# --------------------------------------------------------------------------- 拓扑校验 / 修复


def test_validate_topology_reports_missing_deps() -> None:
    plan = PlanOutput(action="update", tasks=[_task("task3", deps=["task9"])])
    problems = validate_topology(plan, known_ids=set())
    assert problems and "task9" in problems[0]
    # 旧计划里确实有 task9 → 合法
    assert validate_topology(plan, known_ids={"task9"}) == []


def test_repair_topology_drops_missing_deps() -> None:
    tasks = [SubTask(plan_id="task1", deps=["ghost", "task2"]), SubTask(plan_id="task2", deps=[])]
    repaired, notes = repair_topology(tasks, known_ids=set())
    assert repaired[0].deps == ["task2"]
    assert notes and "ghost" in notes[0]


def test_pick_ready_respects_dependencies() -> None:
    tasks = [
        SubTask(plan_id="task1", step_statuses="completed"),
        SubTask(plan_id="task2", deps=["task1"], step_statuses="not_started"),
        SubTask(plan_id="task3", deps=["task2"], step_statuses="not_started"),
        SubTask(plan_id="task4", step_statuses="in_progress"),
    ]
    ready = pick_ready(tasks)
    assert [t.plan_id for t in ready] == ["task2"]
    blocked = next(t for t in tasks if t.plan_id == "task3")
    assert "task2" in blocked.blocked_message


def test_pick_ready_blocks_on_unknown_dep() -> None:
    """依赖指向不存在的任务时不再"静默永久阻塞"，blocked_message 会写明是谁。"""
    tasks = [SubTask(plan_id="task1", deps=["ghost"], step_statuses="not_started")]
    assert pick_ready(tasks) == []
    assert "ghost" in tasks[0].blocked_message


# --------------------------------------------------------------------------- 提交协议


def test_submit_tool_exposes_plan_schema() -> None:
    assert submit_plan.name == SUBMIT_TOOL_NAME
    assert submit_plan.args_schema is PlanOutput
    assert set(submit_plan.args) == {"action", "title", "tasks", "answer"}


def test_parse_submission_validates_args() -> None:
    parsed = parse_submission({"action": "complete", "answer": "答案"})
    assert isinstance(parsed, PlanOutput) and parsed.answer == "答案"
    assert parse_submission({"action": "complete"}) is None  # answer 缺失
    assert parse_submission("not-a-dict") is None


def _after_model(calls: list[dict]) -> dict | None:
    """跑一次「模型输出后」中间件钩子（离线，不需要真实图）。"""
    message = AIMessage(content="", tool_calls=calls)
    return asyncio.run(PlanSubmissionMiddleware().aafter_model({"messages": [message]}, None))


def test_middleware_ends_run_on_valid_submission() -> None:
    """合法提交 → 写入应答 ToolMessage 并结束本轮（不再多花一次模型调用）。"""
    result = _after_model([{"name": SUBMIT_TOOL_NAME, "args": {"action": "complete", "answer": "答案"}, "id": "call-1"}])
    assert result is not None and result["jump_to"] == "end"
    ack = result["messages"][0]
    assert isinstance(ack, ToolMessage) and ack.content == SUBMIT_ACK and ack.tool_call_id == "call-1"


def test_middleware_lets_tool_node_report_invalid_submission() -> None:
    """参数不合法 → 不拦截，交给工具节点把校验错误回给模型自修。"""
    assert _after_model([{"name": SUBMIT_TOOL_NAME, "args": {"action": "bogus"}, "id": "call-1"}]) is None


def test_middleware_ignores_mixed_tool_calls() -> None:
    """与其它工具混用时交给工具节点正常处理（否则会留下悬空 tool call）。"""
    calls = [
        {"name": SUBMIT_TOOL_NAME, "args": {"action": "complete", "answer": "答案"}, "id": "call-1"},
        {"name": "ask_clarification", "args": {"question": "哪个城市？"}, "id": "call-2"},
    ]
    assert _after_model(calls) is None
    assert _after_model([]) is None
