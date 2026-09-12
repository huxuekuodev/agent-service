"""节点共享常量（事件类型统一从 app.agents.events 取，避免魔法字符串）。"""

from app.agents.events import EventType

#: 阶段提示事件类型（保留旧名以兼容既有引用/前端）
THINK_MES = EventType.THINK
