"""会话与对话接口。

- POST /sessions 创建会话
- GET /sessions 列出会话
- DELETE /sessions/{session_id} 删除会话
- POST /sessions/{session_id}/chat 发送消息（SSE 流式）
- POST /sessions/{session_id}/chat/sync 发送消息（同步等待完整回复）

所有接口统一返回信封 ``{data, msg, status}``：
  - status 200 表示成功，msg 为空
  - 业务错误使用 1000 起的自定义编码（见 app/core/response.py），HTTP 状态码始终为 200
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent_service import AgentService
from app.core.log import logger
from app.core.response import (
    INTERNAL_ERROR,
    SESSION_NOT_FOUND,
    SERVICE_NOT_READY,
    BizError,
    err,
    ok,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])


def get_service(request: Request) -> AgentService:
    """从 app.state 获取 AgentService（由 lifespan 注入）。

    生命周期由 FastAPI lifespan 管理：
      - startup 时创建并进入（打开连接池 + 建表）
      - shutdown 时释放
    """
    service = getattr(request.app.state, "agent_service", None)
    if service is None:
        raise BizError(
            SERVICE_NOT_READY,
            "AgentService 未初始化：请通过 FastAPI app 启动（uvicorn app.main:app），"
            "lifespan 会创建并注入 app.state.agent_service。",
        )
    return service


class CreateSessionRequest(BaseModel):
    model_name: str | None = Field(default=None, description="可选，指定模型名")


class CreateSessionResponse(BaseModel):
    session_id: str
    thread_id: str


class ChatRequest(BaseModel):
    message: str = Field(..., description="用户消息")
    stream: bool = Field(default=True, description="是否 SSE 流式返回")


@router.post("")
async def create_session(
    req: CreateSessionRequest, svc: AgentService = Depends(get_service)
) -> dict[str, Any]:
    """创建新会话。"""
    result = svc.create_session(model_name=req.model_name)
    logger.info("创建会话: {}", result["session_id"])
    return ok(CreateSessionResponse(**result).model_dump())


@router.get("")
async def list_sessions(svc: AgentService = Depends(get_service)) -> dict[str, Any]:
    """列出所有活跃会话。"""
    return ok({"sessions": svc.list_sessions()})


@router.delete("/{session_id}")
async def delete_session(session_id: str, svc: AgentService = Depends(get_service)) -> dict[str, Any]:
    """删除会话。"""
    if not svc.delete_session(session_id):
        raise BizError(SESSION_NOT_FOUND, "会话不存在")
    return ok({"session_id": session_id})


@router.post("/{session_id}/chat/sync")
async def chat_sync(session_id: str, req: ChatRequest, svc: AgentService = Depends(get_service)) -> dict[str, Any]:
    """发送消息，等待完整回复。"""
    try:
        messages = await svc.chat(session_id, req.message)
    except ValueError as e:
        raise BizError(SESSION_NOT_FOUND, str(e)) from e
    return ok({"session_id": session_id, "messages": messages})


@router.post("/{session_id}/chat")
async def chat_stream(session_id: str, req: ChatRequest, svc: AgentService = Depends(get_service)) -> StreamingResponse:
    """发送消息，SSE 流式返回。

    每条事件均为统一信封 ``{data, msg, status}``：
      - data: 业务内容（custom / values 快照 / end）
      - msg: 错误提示，成功为空
      - status: 200 成功；业务错误 1000 起（如 1100 会话不存在）
    """

    async def event_gen():
        try:
            async for chunk in svc.stream(session_id, req.message):
                if not isinstance(chunk, dict):
                    continue
                yield f"data: {_serialize(ok(data=chunk))}\n\n"
            yield f"data: {_serialize(ok(data={'type': 'end'}))}\n\n"
        except ValueError as e:
            yield f"data: {_serialize(err(SESSION_NOT_FOUND, str(e)))}\n\n"
        except Exception as e:
            logger.error("对话流失败: {}", e)
            yield f"data: {_serialize(err(INTERNAL_ERROR, str(e)))}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _serialize(chunk: dict) -> str:
    """将信封序列化为 SSE 可传输的 JSON（消息对象转 dict）。"""
    import json

    result: dict[str, Any] = {}
    for k, v in chunk.items():
        if k == "data" and isinstance(v, dict):
            data: dict[str, Any] = {}
            for dk, dv in v.items():
                if dk == "messages" and isinstance(dv, list):
                    data["messages"] = [_msg_to_dict(m) for m in dv]
                else:
                    data[dk] = dv
            result[k] = data
        elif k == "data" and isinstance(v, (tuple, list)) and len(v) == 2:
            msg, meta = v
            result[k] = {"message": _msg_to_dict(msg), "metadata": meta}
        else:
            result[k] = v
    return json.dumps(result, ensure_ascii=False, default=str)


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
