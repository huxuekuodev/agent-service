"""大模型渠道管理。

每个渠道一个实例（instances/ 目录），config.yaml 的 ``models`` 段只引用实例名：

    models:
      default: deepseek
      plan_node_model: deepseek
      evaluate_model: deepseek

对外 API：
  - get_llm_instance / list_llm_instances：实例注册表查询
  - create_chat_model(role, ...)：按角色名构建 ChatModel
  - create_llm / create_llm_with_name / create_execution_llm：按运行配置构建（见 builders.py）
"""

# 导入 instances 包，触发各渠道模块注册（副作用）
from app.llm import instances as _instances  # noqa: F401
from app.llm.base import LLMInstance, get_llm_instance, list_llm_instances, register
from app.llm.builders import create_execution_llm, create_llm, create_llm_with_name
from app.llm.factory import create_chat_model

__all__ = [
    "LLMInstance",
    "register",
    "get_llm_instance",
    "list_llm_instances",
    "create_chat_model",
    "create_llm",
    "create_llm_with_name",
    "create_execution_llm",
]
