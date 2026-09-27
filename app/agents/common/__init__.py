"""跨节点公共设施：事件输出层、LLM 错误分类、中断协议、上下文注入辅助。"""

from app.agents.common.current_time import has_current_time_for_today
from app.agents.common.errors import build_error_fallback_message, classify_llm_error, should_retry
from app.agents.common.events import EventType, Output, StepStatus, ToolCallAccumulator

__all__ = [
    "EventType",
    "Output",
    "StepStatus",
    "ToolCallAccumulator",
    "build_error_fallback_message",
    "classify_llm_error",
    "has_current_time_for_today",
    "should_retry",
]
