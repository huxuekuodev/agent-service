"""LLM 用量采集：一次请求内所有模型调用的 token 统计。

为什么用 callback 而不是流事件：本服务的图只把 ``custom`` 轨暴露给外层
（见 ``GraphAgent.astream``，``stream_mode=["custom"]``），``messages``/``values``
轨拿不到模型 chunk，也就拿不到 ``usage_metadata``。而 LangChain 的 callback 会
自然传播到子图、多轮调用与评估器调用，是唯一可靠且不侵入节点的采集点。

用法::

    usage = UsageCollector()
    async for event in agent.astream(state, thread_id=..., usage=usage):
        ...
    usage.as_dict()   # {"input_tokens": ..., "output_tokens": ..., "cost": ..., "by_model": {...}}

采集口径：
  - 每次 ``on_llm_end`` 记一条调用（流式返回的 usage 是**累计值**，取该次调用的最终值）；
  - ``input_tokens`` 含缓存命中（``input_token_details.cache_read`` 单独统计，便于看 KV 缓存效果）；
  - 费用按 ``token_pricing``（模型角色单价，元 / 1K tokens）计算。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from app.core.log import logger
from app.core.token import compute_token_cost

__all__ = ["LLMCallUsage", "UsageCollector", "resolve_model_role"]


@dataclass
class LLMCallUsage:
    """单次 LLM 调用的用量。"""

    model: str = ""
    """用量归属的模型名（优先解析成 config.yaml 的模型角色名）。"""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    """输入中命中前缀缓存（KV cache）的 token 数。"""

    def add(self, other: LLMCallUsage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.total_tokens += other.total_tokens
        self.cache_read_tokens += other.cache_read_tokens


class UsageCollector(BaseCallbackHandler):
    """收集一次请求内所有 LLM 调用的 token 用量。

    线程/协程安全说明：一次对话请求对应一个实例（在 router 里逐请求创建），
    不跨请求共享，因此无需加锁。
    """

    def __init__(self) -> None:
        super().__init__()
        self._calls: list[LLMCallUsage] = []

    # ------------------------------------------------------------------ callback

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        """记录一次 LLM 调用的用量（解析失败只记日志，绝不影响对话）。"""
        try:
            usage = _parse_llm_result(response)
        except Exception as exc:  # 用量是旁路数据，任何异常都不该冒泡到对话链路
            logger.debug("解析 LLM 用量失败: {}", exc)
            return
        if usage is None:
            return
        self._calls.append(usage)

    # ------------------------------------------------------------------ 汇总

    @property
    def calls(self) -> list[LLMCallUsage]:
        """本次请求的全部模型调用。"""
        return list(self._calls)

    @property
    def by_model(self) -> dict[str, LLMCallUsage]:
        """按模型归并的用量。"""
        merged: dict[str, LLMCallUsage] = {}
        for call in self._calls:
            key = call.model or "unknown"
            if key not in merged:
                merged[key] = LLMCallUsage(model=key)
            merged[key].add(call)
        return merged

    @property
    def calc_cost(self) -> float:
        """本次请求总费用（元，按模型角色单价）。"""
        return round(sum(compute_token_cost(model, u.input_tokens, u.output_tokens) for model, u in self.by_model.items()), 6)

    def as_dict(self) -> dict[str, Any]:
        """汇总结果（用于落库/打点）。"""
        total_in = sum(c.input_tokens for c in self._calls)
        total_out = sum(c.output_tokens for c in self._calls)
        return {
            "input_tokens": total_in,
            "output_tokens": total_out,
            "total_tokens": total_in + total_out,
            "cache_read_tokens": sum(c.cache_read_tokens for c in self._calls),
            "cost": self.calc_cost,
            "llm_calls": len(self._calls),
            "by_model": {
                model: {
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                    "total_tokens": u.total_tokens,
                    "cost": compute_token_cost(model, u.input_tokens, u.output_tokens),
                }
                for model, u in self.by_model.items()
            },
        }

    @property
    def primary_model(self) -> str:
        """用量最大的模型（写入 ``messages.model_role``）。"""
        if not self._calls:
            return ""
        totals: dict[str, int] = {}
        for call in self._calls:
            totals[call.model or "unknown"] = totals.get(call.model or "unknown", 0) + call.total_tokens
        return max(totals.items(), key=lambda kv: kv[1])[0]

    @property
    def has_usage(self) -> bool:
        """是否采集到用量（未采集到时调用方应跳过写入）。"""
        return any(c.total_tokens or c.input_tokens or c.output_tokens for c in self._calls)


# ---------------------------------------------------------------------------
# 解析与模型角色解析
# ---------------------------------------------------------------------------


def _parse_llm_result(response: Any) -> LLMCallUsage | None:
    """从 ``LLMResult`` 解析用量（兼容 ``usage_metadata`` 与 ``llm_output.token_usage``）。"""
    model_name = ""
    input_tokens = output_tokens = total_tokens = cache_read = 0

    for generation_list in getattr(response, "generations", None) or []:
        for generation in generation_list:
            message = getattr(generation, "message", None)
            usage = getattr(message, "usage_metadata", None) if message is not None else None
            if isinstance(usage, dict):
                input_tokens += int(usage.get("input_tokens") or 0)
                output_tokens += int(usage.get("output_tokens") or 0)
                total_tokens += int(usage.get("total_tokens") or 0)
                details = usage.get("input_token_details") or {}
                if isinstance(details, dict):
                    cache_read += int(details.get("cache_read") or 0)
            meta = (getattr(message, "response_metadata", None) or {}) if message is not None else {}
            if isinstance(meta, dict) and not model_name:
                model_name = str(meta.get("model_name") or meta.get("model") or "")

    llm_output = getattr(response, "llm_output", None) or {}
    if isinstance(llm_output, dict):
        token_usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
        if isinstance(token_usage, dict) and not total_tokens:
            input_tokens = int(token_usage.get("prompt_tokens") or token_usage.get("input_tokens") or input_tokens)
            output_tokens = int(token_usage.get("completion_tokens") or token_usage.get("output_tokens") or output_tokens)
            total_tokens = int(token_usage.get("total_tokens") or (input_tokens + output_tokens))
        if not model_name:
            model_name = str(llm_output.get("model_name") or llm_output.get("model") or "")

    if not (input_tokens or output_tokens or total_tokens):
        return None
    return LLMCallUsage(
        model=resolve_model_role(model_name),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens or (input_tokens + output_tokens),
        cache_read_tokens=cache_read,
    )


def resolve_model_role(provider_model: str) -> str:
    """provider 侧模型名 → config.yaml 的模型角色名（用于查单价）。

    例：``Qwen/Qwen3-14B`` → ``default``（因为 ``models.default`` 指向注册了该
    模型名的实例）。解析不到时原样返回，费用按 0 计（不影响 token 统计）。
    """
    if not provider_model:
        return "unknown"
    try:
        from app.config import get_app_config
        from app.llm.base import list_llm_instances

        config = get_app_config()
        instance_names = {inst.model: inst.name for inst in list_llm_instances() if inst.model == provider_model}
        if instance_names:
            for role, instance_name in config.models.items():
                if instance_name in instance_names.values():
                    return role
    except Exception as exc:  # 配置缺失等场景：退回 provider 模型名
        logger.debug("解析模型角色失败: {}", exc)
    return provider_model
