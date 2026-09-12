"""会话层单元测试（离线：回复收集、标题生成、分页游标）。"""

from __future__ import annotations

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from app.session.service import AssistantReplyCollector, _make_cursor, _make_title, _parse_cursor


def _collect(*events: dict) -> AssistantReplyCollector:
    collector = AssistantReplyCollector()
    for event in events:
        collector.feed(event)
    return collector


def _custom(**payload: object) -> dict:
    return {"type": "custom", "data": {**payload}}


# --------------------------------------------------------------------------- 回复收集


def test_answer_event_wins_over_other_tracks() -> None:
    collector = _collect(
        _custom(type="thinking", delta="思考中"),
        {"type": "values", "data": {"messages": [HumanMessage(content="问"), AIMessage(content="快照答案")]}},
        _custom(type="answer", content="最终答复"),
    )
    assert collector.content == "最终答复"
    assert collector.kind == "answer"


def test_clarify_event_becomes_clarify_message() -> None:
    collector = _collect(_custom(type="clarify", content="请补充目标平台", clarification_type="missing_info", options=["iOS", "Android"]))
    assert collector.content == "请补充目标平台"
    assert collector.kind == "clarify"
    assert collector.payload["clarify"]["options"] == ["iOS", "Android"]


def test_snapshot_used_when_no_explicit_event() -> None:
    """values 轨兜底：取最后一条非空 AIMessage，跳过 ToolMessage，越过 HumanMessage 即止。"""
    collector = _collect(
        {
            "type": "values",
            "data": {
                "messages": [
                    HumanMessage(content="问"),
                    AIMessage(content="历史答案"),
                    HumanMessage(content="再问"),
                    ToolMessage(content="工具输出", tool_call_id="t1"),
                    AIMessage(content="最新答案"),
                ]
            },
        }
    )
    assert collector.content == "最新答案"
    assert collector.kind == "answer"


def test_error_event_becomes_error_message() -> None:
    collector = _collect(_custom(type="error", messages="模型超时"))
    assert collector.content == "模型超时"
    assert collector.kind == "error"


def test_token_delta_is_last_resort() -> None:
    collector = _collect({"type": "messages", "data": [AIMessageChunk(content="流式"), {"ignored": True}]})
    assert collector.content == "流式"
    assert collector.kind == "answer"


def test_empty_stream_persists_nothing() -> None:
    collector = _collect({"type": "values", "data": {"messages": []}}, _custom(type="thinking", delta=""))
    assert collector.content == ""
    assert collector.payload == {}


def test_error_kept_as_payload_when_answer_exists() -> None:
    collector = _collect(_custom(type="error", messages="中途重试"), _custom(type="answer", content="最终答复"))
    assert collector.content == "最终答复"
    assert collector.payload["error"] == "中途重试"


# --------------------------------------------------------------------------- 标题 / 游标


def test_make_title_truncates_and_flattens() -> None:
    assert _make_title("  帮我   规划\n一个任务  ") == "帮我 规划 一个任务"
    long_text = "一" * 50
    assert len(_make_title(long_text)) == 20
    assert _make_title("   ") == "新会话"


def test_cursor_roundtrip() -> None:
    session = {"session_id": "abc-123", "last_message_at": "2026-09-12T10:00:00+00:00", "created_at": "2026-09-01T00:00:00+00:00"}
    parsed = _parse_cursor(_make_cursor(session))
    assert parsed == ("2026-09-12T10:00:00+00:00", "abc-123")


def test_cursor_falls_back_to_created_at_and_rejects_garbage() -> None:
    session = {"session_id": "s1", "last_message_at": None, "created_at": "2026-09-01T00:00:00+00:00"}
    assert _parse_cursor(_make_cursor(session)) == ("2026-09-01T00:00:00+00:00", "s1")
    assert _parse_cursor(None) is None
    # 非法游标按首页处理，不打断列表请求
    assert _parse_cursor("garbage") is None
    assert _parse_cursor("|no-stamp") is None
