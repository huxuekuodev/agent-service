"""会话业务编排：会话生命周期 + 对话落库。

职责边界：
  - :mod:`app.session.store` 只做 SQL；本模块负责**业务规则**（归属校验、标题生成、
    游标分页、幂等、落库失败降级）；
  - 图（LangGraph）只负责"生成"，消息的**用户可见事实**由本模块写入业务库。

落库失败策略由 ``business_database.fail_fast`` 决定：
  - ``False``（默认）：记日志并继续，**对话优先**（消息表可从 stream 结果重建或丢失一轮，
    但不能因为数据库抖动让用户发不出消息）；
  - ``True``：抛出 :class:`BizError`，用于对持久化有强一致性要求的部署。
"""

from __future__ import annotations

import uuid
from typing import Any

from app.config import get_app_config
from app.core.log import logger
from app.core.response import SESSION_NOT_FOUND, STORE_UNAVAILABLE, BizError
from app.session import store

__all__ = ["AssistantReplyCollector", "SessionService"]

_TITLE_MAX = 20
"""首条用户消息生成标题时截取的字数。"""
_DEFAULT_TITLE = "新会话"


class SessionService:
    """会话与消息的业务服务（无状态，可全局单例复用）。"""

    def __init__(self) -> None:
        config = get_app_config().business_database
        self._page_size = config.sessions_page_size
        self._messages_page_size = config.messages_page_size
        self._fail_fast = config.fail_fast

    # ------------------------------------------------------------------ 能力探测

    @property
    def available(self) -> bool:
        """业务库是否已配置。"""
        return store.is_available()

    async def aclose(self) -> None:
        """释放业务库连接池（应用 shutdown 时调用）。"""
        await store.aclose()

    # ------------------------------------------------------------------ 会话

    async def create(self, *, user_id: str, title: str = "", model_role: str | None = None) -> dict[str, Any]:
        """创建会话（``checkpoint_thread_id`` = 会话 id，图状态按它持久化）。"""
        session_id = str(uuid.uuid4())
        row = await store.create_session(user_id=user_id, session_id=session_id, title=title or _DEFAULT_TITLE, model_role=model_role)
        logger.info("创建会话: session={} user={}", row["session_id"], user_id)
        return row

    async def get(self, session_id: str, *, user_id: str) -> dict[str, Any] | None:
        """取会话（带归属校验）。"""
        return await store.get_session(session_id, user_id=user_id)

    async def require(self, session_id: str, *, user_id: str) -> dict[str, Any]:
        """取会话，不存在/不属于该用户时抛业务异常。"""
        session = await store.get_session(session_id, user_id=user_id)
        if session is None:
            raise BizError(SESSION_NOT_FOUND, "会话不存在")
        return session

    async def list(
        self,
        *,
        user_id: str,
        limit: int | None = None,
        cursor: str | None = None,
        query: str = "",
    ) -> dict[str, Any]:
        """会话列表（键集分页）。

        Returns:
            ``{"sessions": [...], "next_cursor": str | None, "has_more": bool}``
        """
        size = max(1, min(int(limit or self._page_size), 100))
        rows = await store.list_sessions(user_id=user_id, limit=size + 1, cursor=_parse_cursor(cursor), query=query.strip())
        has_more = len(rows) > size
        items = rows[:size]
        return {
            "sessions": items,
            "has_more": has_more,
            "next_cursor": _make_cursor(items[-1]) if has_more and items else None,
        }

    async def rename(self, session_id: str, *, user_id: str, title: str | None = None, status: str | None = None) -> dict[str, Any]:
        """重命名 / 归档会话。"""
        row = await store.update_session(session_id, user_id=user_id, title=title, status=status)
        if row is None:
            raise BizError(SESSION_NOT_FOUND, "会话不存在")
        return row

    async def delete(self, session_id: str, *, user_id: str) -> dict[str, Any]:
        """逻辑删除会话，返回被删会话（``thread_id`` 供调用方清理 checkpoint）。"""
        row = await store.soft_delete_session(session_id, user_id=user_id)
        if row is None:
            raise BizError(SESSION_NOT_FOUND, "会话不存在")
        logger.info("逻辑删除会话: session={} thread={}", row["session_id"], row["thread_id"])
        return row

    # ------------------------------------------------------------------ 消息

    async def messages(
        self,
        session_id: str,
        *,
        user_id: str,
        limit: int | None = None,
        before_seq: int | None = None,
    ) -> dict[str, Any]:
        """历史消息（倒序取页、正序返回）。"""
        size = max(1, min(int(limit or self._messages_page_size), 200))
        await self.require(session_id, user_id=user_id)
        rows = await store.list_messages(session_id=session_id, user_id=user_id, limit=size, before_seq=before_seq)
        first_seq = rows[0]["seq"] if rows else None
        return {
            "messages": rows,
            "has_more": bool(rows) and first_seq is not None and first_seq > 1,
            "before_seq": first_seq,
        }

    async def record_user_message(self, session: dict[str, Any], *, content: str, client_msg_id: str | None = None) -> dict[str, Any] | None:
        """写入用户消息；首条消息顺带生成标题。"""
        row = await self._safe(
            store.append_message(
                session_id=session["session_id"],
                role="user",
                content=content,
                kind="chat",
                client_msg_id=client_msg_id,
            ),
            what="用户消息",
        )
        if row is not None and int(session.get("message_count") or 0) == 0 and (session.get("title") or _DEFAULT_TITLE) == _DEFAULT_TITLE:
            await self._safe(
                store.update_session(session["session_id"], user_id=session["user_id"], title=_make_title(content)),
                what="会话标题",
            )
        return row

    async def record_assistant_message(
        self,
        session_id: str,
        *,
        content: str,
        kind: str = "answer",
        payload: dict[str, Any] | None = None,
        model_role: str | None = None,
        token_input: int | None = None,
        token_output: int | None = None,
        latency_ms: int | None = None,
        client_msg_id: str | None = None,
    ) -> dict[str, Any] | None:
        """写入助手消息（最终答复 / 澄清问题 / 错误）。"""
        return await self._safe(
            store.append_message(
                session_id=session_id,
                role="assistant",
                content=content,
                kind=kind,
                payload=payload,
                client_msg_id=client_msg_id,
                model_role=model_role,
                token_input=token_input,
                token_output=token_output,
                latency_ms=latency_ms,
            ),
            what=f"助手消息({kind})",
        )

    # ------------------------------------------------------------------ 内部

    async def _safe(self, coro: Any, *, what: str) -> Any:
        """落库容错包装：按 ``fail_fast`` 决定抛错还是降级。"""
        try:
            return await coro
        except BizError:
            raise
        except Exception as exc:
            if self._fail_fast:
                raise BizError(STORE_UNAVAILABLE, f"{what}写入失败：{exc}") from exc
            logger.warning("{}写入失败（已降级，不影响对话）: {}", what, exc)
            return None


class AssistantReplyCollector:
    """从图事件流中收集"该落库的助手回复"。

    图的产出是多轨的（见 ``docs/消息流转与展示方案.md``）：节点会发 ``answer`` /
    ``clarify`` / ``error`` 业务事件，同时 ``values`` 快照里有内部消息、``messages``
    轨有 token 增量。落库只认**用户可见的那一条**，优先级：

        answer 事件 > clarify 事件 > values 里最后一条非空 AIMessage > error 事件 > token 增量

    这样既避免把工具调用/结构化输出 dump 写进历史，又保证任何一条轨道缺失时都有兜底。
    """

    def __init__(self) -> None:
        self._answer = ""
        self._clarify: dict[str, Any] | None = None
        self._error = ""
        self._snapshot = ""
        self._delta = ""

    def feed(self, event: dict[str, Any]) -> None:
        """喂入一条归一化流事件（``{"type": ..., "data": ...}``）。"""
        etype = event.get("type")
        data = event.get("data")
        if etype == "custom" and isinstance(data, dict):
            inner = str(data.get("type") or "")
            if inner == "answer":
                self._answer = str(data.get("content") or "") or self._answer
            elif inner == "clarify":
                self._clarify = {
                    "content": str(data.get("content") or ""),
                    "clarification_type": data.get("clarification_type") or "",
                    "options": list(data.get("options") or []),
                    "context": data.get("context") or "",
                }
            elif inner == "error":
                self._error = str(data.get("messages") or data.get("message") or "") or self._error
            elif inner == "thinking":
                self._delta += str(data.get("delta") or "")
        elif etype == "values" and isinstance(data, dict):
            snapshot = _last_ai_text(data.get("messages"))
            if snapshot:
                self._snapshot = snapshot
        elif etype == "messages":
            self._delta += _chunk_text(data)

    @property
    def content(self) -> str:
        """最终要落库的文本。"""
        if self._answer:
            return self._answer
        if self._clarify and self._clarify.get("content"):
            return self._clarify["content"]
        if self._snapshot:
            return self._snapshot
        if self._error:
            return self._error
        return self._delta.strip()

    @property
    def kind(self) -> str:
        """消息类型：answer / clarify / error（与 messages.kind CHECK 对齐）。"""
        if self._answer or self._snapshot or self._delta.strip():
            return "answer"
        if self._clarify and self._clarify.get("content"):
            return "clarify"
        if self._error:
            return "error"
        return "answer"

    @property
    def payload(self) -> dict[str, Any]:
        """结构化附加信息（澄清选项等）。"""
        out: dict[str, Any] = {}
        if self._clarify:
            out["clarify"] = self._clarify
        if self._error and self._answer:
            out["error"] = self._error
        return out


def _last_ai_text(messages: Any) -> str:
    """从状态快照里取最后一条非空 AIMessage（越过 HumanMessage 即止）。"""
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        name = type(msg).__name__
        if name == "HumanMessage":
            return ""
        if name != "AIMessage":
            continue
        content = getattr(msg, "content", "")
        if isinstance(content, str) and content.strip():
            return content
    return ""


def _chunk_text(data: Any) -> str:
    """从 ``messages`` 轨的 chunk 里取文本增量。"""
    candidates = data if isinstance(data, list) else [data]
    if candidates and isinstance(candidates[0], (list, tuple)):
        candidates = list(candidates[0])
    out: list[str] = []
    for item in candidates:
        content = item if isinstance(item, str) else getattr(item, "content", "")
        if isinstance(content, str) and content:
            out.append(content)
    return "".join(out)


def _make_title(content: str) -> str:
    """首条用户消息 → 会话标题（单行、限长）。"""
    text = " ".join((content or "").split())
    return text[:_TITLE_MAX] or _DEFAULT_TITLE


def _make_cursor(session: dict[str, Any]) -> str:
    """键集分页游标：``最后活跃时间|会话id``。"""
    stamp = session.get("last_message_at") or session.get("created_at") or ""
    return f"{stamp}|{session.get('session_id', '')}"


def _parse_cursor(cursor: str | None) -> tuple[str, str] | None:
    """解析游标；非法游标按"首页"处理，避免分页参数错误打断列表。"""
    if not cursor:
        return None
    stamp, sep, session_id = cursor.partition("|")
    if not sep or not stamp or not session_id:
        return None
    return stamp, session_id
