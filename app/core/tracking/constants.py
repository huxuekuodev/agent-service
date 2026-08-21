"""打点常量：规范打点类型（Content.type）、业务名称（Content.page）与来源（Content.source）。

新增打点时优先复用这里已定义的枚举；确需新类型时在此补充并同步更新
docs/打点设计.md 中的含义说明。
"""

from __future__ import annotations

from enum import StrEnum


class TrackingType(StrEnum):
    """打点类型（Content.type）——这条打点记录的是什么事件。"""

    # ---- 交互 ----
    CLICK = "click"
    """点击。"""
    EXPOSURE = "exposure"
    """曝光（页面/模块被展示）。"""
    VIEW = "view"
    """浏览/查看。"""

    # ---- 会话 ----
    SESSION_START = "session_start"
    """会话开始。"""
    SESSION_END = "session_end"
    """会话结束。"""
    MESSAGE_SEND = "message_send"
    """用户发送消息。"""
    MESSAGE_RECEIVE = "message_receive"
    """助手回复消息。"""

    # ---- 规划 ----
    PLAN_CREATE = "plan_create"
    """创建新计划。"""
    PLAN_UPDATE = "plan_update"
    """更新现有计划。"""
    PLAN_COMPLETE = "plan_complete"
    """计划完成（反思通过给出最终答案）。"""
    CLARIFY = "clarify"
    """发起澄清提问。"""

    # ---- 执行 ----
    STEP_START = "step_start"
    """子任务开始执行。"""
    STEP_COMPLETE = "step_complete"
    """子任务执行完成。"""
    TOOL_CALL = "tool_call"
    """执行节点调用工具。"""
    TOOL_RESULT = "tool_result"
    """工具返回结果。"""

    # ---- 评估 ----
    EVALUATION = "evaluation"
    """LLM-as-Judge 评估。"""

    # ---- 性能 / 错误 ----
    LATENCY = "latency"
    """耗时统计。"""
    ERROR = "error"
    """错误事件。"""
    JOIN = "join"
    """一次完整请求（调用模型到返回）。"""
    TOKEN_USAGE = "token_usage"
    """Token 消耗（一次请求的输入/输出 token）。"""


class TrackingPage(StrEnum):
    """业务名称（Content.page）——这条打点属于哪个业务模块。

    Ext 各槽位的含义按 page 分配，见 docs/打点设计.md。
    """

    CHAT = "chat"
    """对话。"""
    PLAN = "plan"
    """规划节点。"""
    EXECUTE = "execute"
    """执行节点。"""
    EVALUATION = "evaluation"
    """评估器。"""
    KNOWLEDGE = "knowledge"
    """知识库。"""
    SYSTEM = "system"
    """系统/框架层。"""
    CALL_MODEL = "call_model"
    """调用模型（一次完整请求）。"""
    TOKEN = "token"
    """Token 消耗（p0=总 token，p1=输入，p2=输出，p3=费用）。"""


class TrackingSource(StrEnum):
    """来源（Content.source）——这条打点从哪产生。"""

    WEB = "web"
    """Web 前端。"""
    SERVER = "server"
    """服务端。"""
    APP = "app"
    """移动端/客户端。"""
    INTERNAL = "internal"
    """内部任务/后台。"""
