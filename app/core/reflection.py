"""动态模块加载（独立服务版）。

复制自 deerflow.reflection，去掉对 config 的依赖。
"""

from __future__ import annotations

from importlib import import_module
from typing import TypeVar, cast

T = TypeVar("T")


def resolve_variable(
    variable_path: str,
    expected_type: type[T] | tuple[type, ...] | None = None,
) -> T:
    """从路径解析变量。

    Args:
        variable_path: 变量路径，如 "module.path:variable_name"。
        expected_type: 可选的类型校验。

    Returns:
        解析出的变量。

    Raises:
        ImportError: 模块路径无效或属性不存在。
        ValueError: 解析结果不满足类型校验。
    """
    try:
        module_path, variable_name = variable_path.rsplit(":", 1)
    except ValueError as err:
        raise ImportError(f"{variable_path} doesn't look like a variable path. Example: module.path:variable_name") from err

    try:
        module = import_module(module_path)
    except ImportError as err:
        raise ImportError(f"Could not import module {module_path}: {err}") from err

    try:
        variable = getattr(module, variable_name)
    except AttributeError as err:
        raise ImportError(f"Module {module_path} has no attribute {variable_name}") from err

    if expected_type is not None and not isinstance(variable, expected_type):
        raise ValueError(f"Variable {variable_path} resolved to {type(variable).__name__}, expected {expected_type}")
    return cast(T, variable)


def resolve_class(  # noqa: UP047
    variable_path: str,
    base_class: type[T] | tuple[type[T], ...],
) -> type[T]:
    """从路径解析类，并校验是否为 base_class 的子类。"""
    cls = resolve_variable(variable_path, type)
    if not isinstance(cls, type) or not issubclass(cls, base_class):
        raise ValueError(f"Variable {variable_path} is not a subclass of {base_class}")
    return cls
