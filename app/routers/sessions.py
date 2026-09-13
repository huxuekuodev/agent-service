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
    INTERRUPT_MISMATCH,
    INTERRUPT_PENDING,
    NO_PENDING_INTERRUPT,
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
from app.voice.audio import plan_review_speech
from app.voice.service import VoiceError, get_voice_service

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
    voice: bool = Field(default=False, description="是否语音通话模式：答复会按句合成语音并以 voice_audio 事件回传")


class ResumeAnswerItem(BaseModel):
    """单个问题的答复（与 ask/answers 协议对齐）。"""

    id: str = Field(default="", description="问题 id（来自 interrupt 载荷里的 questions[].id）")
    selected: list[str] = Field(default_factory=list, description="选中的选项（当前确认卡片不使用，留作多问题扩展）")
    custom: str = Field(default="", description="用户自由输入的意见；留空表示按原样继续")


class ResumeRequest(BaseModel):
    """恢复被挂起的运行（interrupt → resume）。

    - ``answers`` 为空 = 用户直接继续（等价于没有意见）；
    - ``custom`` 非空 = 用户提了意见，节点会回到规划节点重新规划；
    - ``interrupt_id`` 可选：带上可校验卡片是否过期（旧卡片的答复会被拒绝）。
    """

    answers: list[ResumeAnswerItem] = Field(default_factory=list, description="各问题的答复")
    interrupt_id: str = Field(default="", description="可选：要恢复的中断 id（与当前挂起不一致时拒绝）")
    client_msg_id: str | None = Field(default=None, description="可选：用户答复消息的幂等 id")
    voice: bool = Field(default=False, description="是否语音通话模式（同上）")


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
    svc: AgentService = Depends(get_service),
) -> dict[str, Any]:
    """会话详情（含 ``pending_interrupt``：刷新页面后据此恢复确认卡片）。"""
    _require_store()
    session = await sessions.require(session_id, user_id=str(user["user_id"]))
    session["pending_interrupt"] = await svc.pending_interrupt(session["thread_id"])
    return ok(session)


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
    await _prepare_turn(svc, session)
    await sessions.record_user_message(session, content=req.message, client_msg_id=req.client_msg_id)

    collector = AssistantReplyCollector()
    usage = UsageCollector()
    started = time.perf_counter()
    try:
        async for event in svc.stream(session["thread_id"], req.message, usage=usage, voice=req.voice):
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
    await _prepare_turn(svc, session)
    await sessions.record_user_message(session, content=req.message, client_msg_id=req.client_msg_id)
    thread_id = session["thread_id"]

    async def event_gen():
        collector = AssistantReplyCollector()
        usage = UsageCollector()
        started = time.perf_counter()
        failed = False
        interrupt_payload: dict[str, Any] = {}
        try:
            async for chunk in svc.stream(thread_id, req.message, usage=usage, voice=req.voice):
                if not isinstance(chunk, dict):
                    continue
                collector.feed(chunk)
                if chunk.get("type") == "interrupt":
                    interrupt_payload = (chunk.get("data") or {}).get("payload") or {}
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
            if req.voice:
                # 语音模式：把答复（或计划确认提示）按句合成，逐块回传供前端排队播放
                speech_text = plan_review_speech(interrupt_payload) if interrupt_payload else collector.content
                async for frame in _voice_frames(speech_text):
                    yield frame

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{session_id}/resume")
async def resume_session(
    session_id: str,
    req: ResumeRequest,
    user: dict[str, Any] = Depends(current_user),
    svc: AgentService = Depends(get_service),
    sessions: SessionService = Depends(get_session_service),
) -> StreamingResponse:
    """恢复被 ``interrupt()`` 挂起的运行（SSE，同一套信封与落库流程）。

    与 ``/chat`` 的区别：这里**不产生新的一轮对话**，而是把用户的答复交给挂起的节点继续执行。
    挂起状态下用 ``/chat`` 发消息不会生效（LangGraph 会重跑被打断的节点并生成新的中断），
    因此 ``/chat`` 在挂起时会返回 1103 让前端改走本接口。
    """
    _require_store()
    session = await sessions.require(session_id, user_id=str(user["user_id"]))
    pending = await svc.prepare_turn(session["thread_id"])
    if pending is None:
        raise BizError(NO_PENDING_INTERRUPT, "该会话当前没有等待确认的中断")
    if req.interrupt_id and req.interrupt_id != pending.get("interrupt_id"):
        raise BizError(INTERRUPT_MISMATCH, "确认卡片已过期（中断 id 不一致），请刷新会话后重试")

    answers = [a.model_dump() for a in req.answers]
    payload = {"interrupt_id": pending.get("interrupt_id", ""), "answers": answers}
    feedback = "\n".join(a.custom.strip() for a in req.answers if a.custom.strip())

    # 用户答复作为一条 user 消息落库（历史里能看到"我提了什么意见"）
    await sessions.record_user_message(
        session,
        content=feedback or "（继续执行）",
        client_msg_id=req.client_msg_id,
    )
    thread_id = session["thread_id"]

    async def event_gen():
        collector = AssistantReplyCollector()
        usage = UsageCollector()
        started = time.perf_counter()
        failed = False
        interrupt_payload: dict[str, Any] = {}
        try:
            async for chunk in svc.stream(thread_id, resume=payload, usage=usage, voice=req.voice):
                if not isinstance(chunk, dict):
                    continue
                collector.feed(chunk)
                if chunk.get("type") == "interrupt":
                    interrupt_payload = (chunk.get("data") or {}).get("payload") or {}
                yield f"data: {_serialize(ok(data=chunk))}\n\n"
            yield f"data: {_serialize(ok(data={'type': 'end'}))}\n\n"
        except Exception as e:
            failed = True
            logger.error("恢复运行失败: session={} err={}", session_id, e)
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
            if req.voice:
                # 语音模式：把答复（或计划确认提示）按句合成，逐块回传供前端排队播放
                speech_text = plan_review_speech(interrupt_payload) if interrupt_payload else collector.content
                async for frame in _voice_frames(speech_text):
                    yield frame

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _voice_frames(text: str):
    """把答复按句合成语音，逐块产出 SSE 帧（``voice_audio``）。

    - **逐句合成**：首块合成完就发，长答复不必等整段（实测 TTS 延迟随文本线性增长）；
    - 合成失败给出 ``voice_unavailable``，前端退化为浏览器内置朗读，通话不会静默；
    - 结束时补一条 ``voice_end``，前端据此回到"聆听中"。
    """
    import base64

    voice = get_voice_service()
    if not voice.available:
        yield f"data: {_serialize(ok(data={'type': 'voice_unavailable', 'msg': voice.unavailable_reason()}))}\n\n"
        return
    emitted = False
    try:
        async for chunk in voice.synthesize_chunks(text or ""):
            if not chunk.audio:
                continue
            emitted = True
            payload = {"type": "voice_audio", "seq": chunk.index, "text": chunk.text, "format": "mp3", "audio": base64.b64encode(chunk.audio).decode("ascii"), "final": chunk.final}
            yield f"data: {_serialize(ok(data=payload))}\n\n"
    except VoiceError as exc:
        logger.warning("语音合成失败，降级为浏览器朗读: {}", exc)
        yield f"data: {_serialize(ok(data={'type': 'voice_unavailable', 'msg': str(exc)}))}\n\n"
    if not emitted:
        yield f"data: {_serialize(ok(data={'type': 'voice_unavailable', 'msg': '本轮没有可播报的内容'}))}\n\n"
    yield f"data: {_serialize(ok(data={'type': 'voice_end'}))}\n\n"


async def _prepare_turn(svc: AgentService, session: dict[str, Any]) -> None:
    """回合开始前的状态准备（一次 checkpoint 读取）：

    - 有挂起中断 → 拒绝本回合（引导走 /resume），避免"看起来成功但答复被丢弃"；
    - 有上一轮被打断留下的 in_progress 任务 → 复位，避免会话假死。
    """
    pending = await svc.prepare_turn(session["thread_id"])
    if pending is not None:
        raise BizError(
            INTERRUPT_PENDING,
            "该会话正在等待你的确认：请提交确认卡片（POST /sessions/{id}/resume），而不是发送新消息。",
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
