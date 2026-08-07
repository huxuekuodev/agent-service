"""会话与对话接口。

- POST /sessions 创建会话
- GET /sessions 列出会话
- DELETE /sessions/{session_id} 删除会话
- POST /sessions/{session_id}/chat 发送消息（SSE 流式）
- POST /sessions/{session_id}/chat/sync 发送消息（同步等待完整回复）
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent_service import AgentService
from app.core.log import logger

router = APIRouter(prefix="/sessions", tags=["sessions"])

# 全局服务实例（由 main.py 注入或单例）
def get_service() -> AgentService:
    """获取全局 AgentService（生命周期由 main.py 的 startup/shutdown 管理）。

    若未初始化（直接调用 router 而非通过 main 启动），则自动创建（memory 模式可用）。
    """
    from app.main import _service

    if _service is None:
        raise RuntimeError(
            "AgentService 未初始化：请通过 FastAPI app 启动（uvicorn app.main:app），"
            "startup 事件会创建并进入 service 生命周期。"
        )
    return _service


class CreateSessionRequest(BaseModel):
    model_name: str | None = Field(default=None, description="可选，指定模型名")


class CreateSessionResponse(BaseModel):
    session_id: str
    thread_id: str


class ChatRequest(BaseModel):
    message: str = Field(..., description="用户消息")
    stream: bool = Field(default=True, description="是否 SSE 流式返回")


@router.post("", response_model=CreateSessionResponse)
async def create_session(req: CreateSessionRequest) -> CreateSessionResponse:
    """创建新会话。"""
    svc = get_service()
    result = svc.create_session(model_name=req.model_name)
    logger.info("创建会话: {}", result["session_id"])
    return CreateSessionResponse(**result)


@router.get("")
async def list_sessions() -> dict:
    """列出所有活跃会话。"""
    svc = get_service()
    return {"sessions": svc.list_sessions()}


@router.delete("/{session_id}")
async def delete_session(session_id: str) -> dict:
    """删除会话。"""
    svc = get_service()
    if not svc.delete_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"success": True, "session_id": session_id}


@router.post("/{session_id}/chat/sync")
async def chat_sync(session_id: str, req: ChatRequest) -> dict:
    """发送消息，等待完整回复。"""
    svc = get_service()
    try:
        messages = await svc.chat(session_id, req.message)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"session_id": session_id, "messages": messages}


@router.post("/{session_id}/chat")
async def chat_stream(session_id: str, req: ChatRequest) -> StreamingResponse:
    """发送消息，SSE 流式返回。

    Yields:
        - custom: 规划/执行状态
        - values: 完整 state 快照（含新消息）
        - end: 结束
    """
    svc = get_service()

    async def event_gen():
        try:
            async for chunk in svc.stream(session_id, req.message):
                if not isinstance(chunk, dict):
                    continue
                yield f"data: {_serialize(chunk)}\n\n"
            yield 'data: {"type": "end"}\n\n'
        except ValueError as e:
            yield f'data: {{"type": "error", "detail": {str(e)!r}}}\n\n'
        except Exception as e:
            logger.error("对话流失败: {}", e)
            yield f'data: {{"type": "error", "detail": {str(e)!r}}}\n\n'

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _serialize(chunk: dict) -> str:
    """将 chunk 序列化为 SSE 可传输的 JSON（消息对象转 dict）。"""
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
