"""LLM 渠道实例集合。

本目录下每个模块注册一个 LLMInstance（调用 ``app.llm.base.register``）。
新增渠道 = 新增模块并注册，无需改其他代码；注册在导入本包时自动完成。
"""

from __future__ import annotations

import importlib
import pkgutil

from app.llm.base import get_llm_instance, list_llm_instances, register

__all__ = ["register", "get_llm_instance", "list_llm_instances"]

for _module in pkgutil.iter_modules(__path__):
    importlib.import_module(f"{__name__}.{_module.name}")
