"""LLM 用量采集单元测试（离线：解析 LLMResult、模型角色解析、汇总口径）。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from app.llm.usage import UsageCollector, resolve_model_role


def _result(model: str, *, input_tokens: int, output_tokens: int, cache_read: int = 0, token_usage: dict | None = None) -> LLMResult:
    message = AIMessage(
        content="ok",
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "input_token_details": {"cache_read": cache_read},
        },
        response_metadata={"model_name": model},
    )
    return LLMResult(generations=[[ChatGeneration(message=message)]], llm_output={"token_usage": token_usage} if token_usage else {})


def test_collector_aggregates_total_and_by_model() -> None:
    collector = UsageCollector()
    collector.on_llm_end(_result("Qwen/Qwen3-14B", input_tokens=100, output_tokens=10, cache_read=60))
    collector.on_llm_end(_result("Qwen/Qwen3-14B", input_tokens=200, output_tokens=20))
    summary = collector.as_dict()

    assert summary["input_tokens"] == 300
    assert summary["output_tokens"] == 30
    assert summary["total_tokens"] == 330
    assert summary["cache_read_tokens"] == 60
    assert summary["llm_calls"] == 2
    assert list(summary["by_model"]) == [resolve_model_role("Qwen/Qwen3-14B")]
    assert collector.has_usage is True


def test_collector_falls_back_to_llm_output_token_usage() -> None:
    """部分渠道只在 llm_output 里给 usage（没有 usage_metadata）。"""
    collector = UsageCollector()
    response = LLMResult(
        generations=[[ChatGeneration(message=AIMessage(content="ok"))]],
        llm_output={"token_usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}, "model_name": "custom-model"},
    )
    collector.on_llm_end(response)
    summary = collector.as_dict()
    assert (summary["input_tokens"], summary["output_tokens"], summary["total_tokens"]) == (12, 3, 15)


def test_collector_ignores_usage_less_and_broken_responses() -> None:
    collector = UsageCollector()
    collector.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(content="hi"))]]))
    collector.on_llm_end(SimpleNamespace(generations=[SimpleNamespace()]))  # 结构异常
    assert collector.has_usage is False
    assert collector.as_dict()["llm_calls"] == 0


def test_primary_model_prefers_largest_usage() -> None:
    collector = UsageCollector()
    collector.on_llm_end(_result("small-model", input_tokens=10, output_tokens=1))
    collector.on_llm_end(_result("big-model", input_tokens=5000, output_tokens=100))
    assert collector.primary_model == resolve_model_role("big-model")


def test_cost_is_zero_for_unpriced_model() -> None:
    collector = UsageCollector()
    collector.on_llm_end(_result("完全不存在的模型", input_tokens=1000, output_tokens=1000))
    assert collector.as_dict()["cost"] == 0.0


def test_resolve_model_role_unknown_model_returns_itself() -> None:
    assert resolve_model_role("不存在的模型-XYZ") == "不存在的模型-XYZ"
    assert resolve_model_role("") == "unknown"


def test_calls_snapshot_is_a_copy() -> None:
    collector = UsageCollector()
    collector.on_llm_end(_result("m", input_tokens=1, output_tokens=1))
    snapshot: list[Any] = collector.calls
    snapshot.clear()
    assert len(collector.calls) == 1
