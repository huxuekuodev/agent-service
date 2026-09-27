"""主图编排：无状态编译图（GraphAgent）与运行时上下文（GraphContext）。

只导出 GraphContext：GraphAgent 会 import 各节点，节点又依赖本包的 GraphContext，
导出它会形成循环导入。需要 GraphAgent 时显式 ``from app.agents.graph.agent import GraphAgent``。
"""

from app.agents.graph.context import GraphContext

__all__ = ["GraphContext"]
