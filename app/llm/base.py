"""LLM 实例（渠道）数据模型与注册表。

每个「渠道/实例」在 app/llm/instances/ 下用一个模块注册一套完整配置
（模型类、模型名、API Key 环境变量、端点、超时等），config.yaml 的
``models`` 段只做「角色名 → 实例名」的引用，不再重复写模型参数。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["LLMInstance", "register", "get_llm_instance", "list_llm_instances"]


@dataclass(frozen=True)
class LLMInstance:
    """一个 LLM 渠道实例的完整配置（一处定义，多处引用）。"""

    name: str
    """实例唯一名（config.yaml 的 models 段引用它）。"""
    use: str
    """模型类 import 路径，如 langchain_deepseek:ChatDeepSeek。"""
    model: str
    """provider 侧模型名。"""
    api_key_env: str = ""
    """API Key 的环境变量名（不存明文；构建时才读取，改 key 无需重启）。"""
    base_url: str = ""
    """自定义端点；空字符串表示官方端点。"""
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: float = 60.0
    max_retries: int = 2
    supports_thinking: bool = False
    """是否支持 thinking（规划/执行节点是否可开启）。"""
    supports_vision: bool = False
    """是否支持视觉输入（知识库图片转文字用）。"""
    display_name: str = ""
    """展示名（日志/UI）。"""

    @property
    def api_key(self) -> str:
        """按 api_key_env 从环境变量取 API Key（空表示未配置）。"""
        return os.getenv(self.api_key_env, "") if self.api_key_env else ""


_INSTANCES: dict[str, LLMInstance] = {}


def register(instance: LLMInstance) -> LLMInstance:
    """注册一个 LLM 实例（instances/ 下各模块在导入时调用）。"""
    if instance.name in _INSTANCES:
        raise ValueError(f"LLM 实例名重复: {instance.name!r}")
    _INSTANCES[instance.name] = instance
    return instance


def get_llm_instance(name: str) -> LLMInstance:
    """按实例名取实例；未注册时抛出带可用清单的错误。"""
    if name not in _INSTANCES:
        raise ValueError(f"LLM 实例 {name!r} 未注册，可用实例: {sorted(_INSTANCES)}")
    return _INSTANCES[name]


def list_llm_instances() -> list[LLMInstance]:
    """列出全部已注册实例。"""
    return list(_INSTANCES.values())
