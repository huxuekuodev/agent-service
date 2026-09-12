"""会话与消息持久化（业务库）。

模块划分：
  - :mod:`app.session.store`   业务库连接池 + 表 CRUD（唯一的 SQL 出口）
  - :mod:`app.session.service` 会话业务编排（创建/列表/历史/逻辑删除 + checkpoint 清理）

业务库与 LangGraph checkpointer 库分离：``messages`` 是用户可见事实的长期真相，
checkpoint 只管 agent 运行态；两者通过 ``sessions.checkpoint_thread_id`` 关联。
"""

from __future__ import annotations

from app.session.service import AssistantReplyCollector, SessionService

__all__ = ["AssistantReplyCollector", "SessionService"]
