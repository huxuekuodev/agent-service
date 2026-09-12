"""会话与对话接口（账号密码 + JWT 鉴权，全部按 user_id 隔离）。

会话管理：
  - ``POST   /sessions``                    创建会话
  - ``GET    /sessions``                    会话列表（键集分页 + 标题搜索）
  - ``GET    /sessions/{id}``               会话详情
  - ``PATCH  /sessions/{id}``               重命名 / 归档
  - ``DELETE /sessions/{id}``               逻辑删除（并清理该 thread 的 checkpoint）
  - ``GET    /sessions/{id}/messages``      历史消息（倒序取页、正序返回）

对话：
  - ``POST   /sessions/{id}/chat``          SSE 流式（流结束落库 user + assistant/澄清/错误）
  - ``POST   /sessions/{id}/chat/sync``     同步等待完整回复（同样落库）

所有接口统一信封 ``{data, msg, status}``，HTTP 始终 200；未登录 1200，令牌失效 1201/1202。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent_service import AgentService
from app.auth.deps import current_user
from app.core.log import logger
from app.core.response import (
    INTERNAL_ERROR,
    SERVICE_NOT_READY,
    SESSION_NOT_FOUND,
    STORE_UNAVAILABLE,
    BizError,
    err,
    ok,
)
from app.llm.usage import UsageCollector
from app.monitor.usage import record_usage
from app.session import AssistantReplyCollector, SessionService
from app.session import store as session_store

router = APIRouter(prefix="/sessions", tags=["sessions"])


# ---------------------------------------------------------------------------
# 依赖
# ---------------------------------------------------------------------------


def get_service(request: Request) -> AgentService:
    """从 app.state 获取 AgentService（由 lifespan 注入）。"""
    service = getattr(request.app.state, "agent_service", None)
    if service is None:
        raise BizError(
            SERVICE_NOT_READY,
            "AgentService 未初始化：请通过 FastAPI app 启动（uvicorn app.main:app），lifespan 会创建并注入 app.state.agent_service。",
        )
    return service


def get_session_service(request: Request) -> SessionService:
    """从 app.state 获取 SessionService（由 lifespan 注入；缺失时惰性构造）。"""
    service = getattr(request.app.state, "session_service", None)
    if service is None:
        service = SessionService()
        request.app.state.session_service = service
    return service


def _require_store() -> None:
    """业务库未配置时给出明确指引（而非 500）。"""
    if not session_store.is_available():
        raise BizError(STORE_UNAVAILABLE, "会话持久化依赖业务库：请配置 business_database.postgres_url / .env BUSINESS_DATABASE_URL")


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    title: str = Field(default="", max_length=200, description="会话标题，缺省“新会话”")
    model_role: str | None = Field(default=None, description="可选，指定模型角色（config.models 的 key）")


class UpdateSessionRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200, description="新标题")
    status: str | None = Field(default=None, description="active | archived")


class ChatRequest(BaseModel):
    message: str = Field(..., description="用户消息")
    client_msg_id: str | None = Field(default=None, description="前端消息幂等 id（重发不产生重复消息）")


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------


@router.post("")
async def create_session(
    req: CreateSessionRequest,
    user: dict[str, Any] = Depends(current_user),
    sessions: SessionService = Depends(get_session_service),
) -> dict[str, Any]:
    """创建新会话（归属当前登录用户）。"""
    _require_store()
    row = await sessions.create(user_id=str(user["user_id"]), title=req.title, model_role=req.model_role)
    return ok(row)


@router.get("")
async def list_sessions(
    limit: int | None = None,
    cursor: str | None = None,
    q: str = "",
    user: dict[str, Any] = Depends(current_user),
    sessions: SessionService = Depends(get_session_service),
) -> dict[str, Any]:
    """列出当前用户的会话（按最后活跃时间倒序，键集分页）。"""
    _require_store()
    return ok(await sessions.list(user_id=str(user["user_id"]), limit=limit, cursor=cursor, query=q))


@router.get("/{session_id}")
async def get_session(
    session_id: str,
    user: dict[str, Any] = Depends(current_user),
    sessions: SessionService = Depends(get_session_service),
) -> dict[str, Any]:
    """会话详情。"""
    _require_store()
    return ok(await sessions.require(session_id, user_id=str(user["user_id"])))


@router.patch("/{session_id}")
async def update_session(
    session_id: str,
    req: UpdateSessionRequest,
    user: dict[str, Any] = Depends(current_user),
    sessions: SessionService = Depends(get_session_service),
) -> dict[str, Any]:
    """重命名 / 归档会话。"""
    _require_store()
    if req.status is not None and req.status not in ("active", "archived"):
        raise BizError(1000, "status 仅支持 active / archived")
    return ok(await sessions.rename(session_id, user_id=str(user["user_id"]), title=req.title, status=req.status))


@router.delete("/{session_id}")
async def delete_session(
    session_id: str,
    user: dict[str, Any] = Depends(current_user),
    sessions: SessionService = Depends(get_session_service),
    svc: AgentService = Depends(get_service),
) -> dict[str, Any]:
    """逻辑删除会话：业务库置 ``deleted_at``，同时清理该 thread 的 checkpoint。

    业务库消息**保留**（审计/合规），仅对外不可见；checkpoint 属于运行态，直接删。
    """
    _require_store()
    row = await sessions.delete(session_id, user_id=str(user["user_id"]))
    thread_cleared = await svc.delete_thread(row["thread_id"])
    return ok({"session_id": row["session_id"], "thread_id": row["thread_id"], "checkpoint_cleared": thread_cleared})


@router.get("/{session_id}/messages")
async def get_messages(
    session_id: str,
    limit: int | None = None,
    before_seq: int | None = None,
    user: dict[str, Any] = Depends(current_user),
    sessions: SessionService = Depends(get_session_service),
) -> dict[str, Any]:
    """历史消息（倒序取页、正序返回；``before_seq`` 用于上滑加载更早）。"""
    _require_store()
    return ok(await sessions.messages(session_id, user_id=str(user["user_id"]), limit=limit, before_seq=before_seq))


# ---------------------------------------------------------------------------
# 对话
# ---------------------------------------------------------------------------


@router.post("/{session_id}/chat/sync")
async def chat_sync(
    session_id: str,
    req: ChatRequest,
    user: dict[str, Any] = Depends(current_user),
    svc: AgentService = Depends(get_service),
    sessions: SessionService = Depends(get_session_service),
) -> dict[str, Any]:
    """发送消息，等待完整回复并落库。"""
    _require_store()
    session = await sessions.require(session_id, user_id=str(user["user_id"]))
    await sessions.record_user_message(session, content=req.message, client_msg_id=req.client_msg_id)

    collector = AssistantReplyCollector()
    usage = UsageCollector()
    started = time.perf_counter()
    try:
        async for event in svc.stream(session["thread_id"], req.message, usage=usage):
            collector.feed(event)
    except Exception as exc:
        logger.error("同步对话失败: session={} err={}", session_id, exc)
        await sessions.record_assistant_message(session_id, content=str(exc), kind="error", payload={"error": str(exc)})
        raise BizError(INTERNAL_ERROR, str(exc)) from exc

    latency_ms = int((time.perf_counter() - started) * 1000)
    await _persist_reply(sessions, session_id, collector, latency_ms, usage=usage, user_id=str(user["user_id"]))
    return ok(
        {
            "session_id": session_id,
            "messages": [{"type": "assistant", "content": collector.content, "kind": collector.kind}],
            "usage": usage.as_dict(),
        }
    )


@router.post("/{session_id}/chat")
async def chat_stream(
    session_id: str,
    req: ChatRequest,
    user: dict[str, Any] = Depends(current_user),
    svc: AgentService = Depends(get_service),
    sessions: SessionService = Depends(get_session_service),
) -> StreamingResponse:
    """发送消息，SSE 流式返回；流结束（含失败）后把本轮对话落库。

    每条事件均为统一信封 ``{data, msg, status}``：
      - data: 业务内容（custom / values 快照 / end）
      - msg: 错误提示，成功为空
      - status: 200 成功；业务错误 1000 起（如 1100 会话不存在、1200 未登录）
    """
    _require_store()
    session = await sessions.require(session_id, user_id=str(user["user_id"]))
    await sessions.record_user_message(session, content=req.message, client_msg_id=req.client_msg_id)
    thread_id = session["thread_id"]

    async def event_gen():
        collector = AssistantReplyCollector()
        usage = UsageCollector()
        started = time.perf_counter()
        failed = False
        try:
            async for chunk in svc.stream(thread_id, req.message, usage=usage):
                if not isinstance(chunk, dict):
                    continue
                collector.feed(chunk)
                yield f"data: {_serialize(ok(data=chunk))}\n\n"
            yield f"data: {_serialize(ok(data={'type': 'end'}))}\n\n"
        except ValueError as e:
            failed = True
            collector.feed({"type": "custom", "data": {"type": "error", "messages": str(e)}})
            yield f"data: {_serialize(err(SESSION_NOT_FOUND, str(e)))}\n\n"
        except Exception as e:
            failed = True
            logger.error("对话流失败: {}", e)
            collector.feed({"type": "custom", "data": {"type": "error", "messages": str(e)}})
            yield f"data: {_serialize(err(INTERNAL_ERROR, str(e)))}\n\n"
        finally:
            latency_ms = int((time.perf_counter() - started) * 1000)
            await _persist_reply(
                sessions,
                session_id,
                collector,
                latency_ms,
                force_kind="error" if failed else None,
                usage=usage,
                user_id=str(user["user_id"]),
            )

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _persist_reply(
    sessions: SessionService,
    session_id: str,
    collector: AssistantReplyCollector,
    latency_ms: int,
    *,
    force_kind: str | None = None,
    usage: UsageCollector | None = None,
    user_id: str = "",
) -> None:
    """把本轮助手回复落库，并记录 token 用量（按问题 + 按用户）。

    无可见回复时跳过消息落库，但仍然记账：模型可能已经消耗了 token
    （例如只调了工具就失败），漏记会让用量统计偏小。
    """
    usage_summary = usage.as_dict() if usage is not None else {}
    payload = dict(collector.payload)
    if usage_summary.get("llm_calls"):
        payload["usage"] = usage_summary

    content = collector.content.strip()
    if content:
        await sessions.record_assistant_message(
            session_id,
            content=content,
            kind=force_kind or collector.kind,
            payload=payload,
            model_role=(usage.primary_model if usage is not None else None) or None,
            token_input=usage_summary.get("input_tokens"),
            token_output=usage_summary.get("output_tokens"),
            latency_ms=latency_ms,
        )
    else:
        logger.debug("本轮无可见回复，跳过消息落库: session={}", session_id)

    if user_id and usage is not None and usage.has_usage:
        await record_usage(user_id=user_id, session_id=session_id, usage=usage_summary, model_role=usage.primary_model)


def _serialize(chunk: dict) -> str:
    """将信封序列化为 SSE 可传输的 JSON。

    任意深度嵌套的 LangChain 消息对象都会通过 :func:`_json_default` 转成 dict，
    其余不可序列化对象降级为 str。
    """
    import json

    return json.dumps(chunk, ensure_ascii=False, default=_json_default)


def _json_default(obj: Any) -> Any:
    """JSON 兜底：LangChain 消息对象转 dict，其余转 str。"""
    if hasattr(obj, "content"):
        return _msg_to_dict(obj)
    return str(obj)


def _msg_to_dict(msg: Any) -> dict:
    """LangChain 消息转 dict。"""
    content = getattr(msg, "content", "")
    if isinstance(content, list):
        content = " ".join(str(c) for c in content)
    d: dict[str, Any] = {
        "type": type(msg).__name__,
        "content": str(content),
        "name": getattr(msg, "name", None),
        "id": getattr(msg, "id", None),
    }
    tool_calls = getattr(msg, "tool_calls", None) or []
    if tool_calls:
        d["tool_calls"] = [{"name": tc.get("name"), "args": tc.get("args"), "id": tc.get("id")} for tc in tool_calls]
    return d
