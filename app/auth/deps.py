"""FastAPI 认证依赖：从 ``Authorization: Bearer <token>`` 解析当前用户。

用法::

    @router.get("/x")
    async def handler(user: dict = Depends(current_user)) -> ...: ...

未登录/令牌失效抛 ``BizError``（HTTP 仍 200，业务码 1200/1201/1202），由全局异常
处理器转成统一信封 ``{data, msg, status}``。
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from app.core.log import logger
from app.core.response import UNAUTHORIZED, BizError

__all__ = ["current_user", "optional_user"]


def _bearer_token(request: Request) -> str:
    """取请求头里的 Bearer 令牌（缺失/格式错误返回空串）。"""
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return ""
    return token.strip()


def _resolve_service(request: Request):
    """取认证服务（lifespan 注入 app.state）。"""
    service = getattr(request.app.state, "auth_service", None)
    if service is None:
        # 未注入时惰性构造：让脚本/单测直接 import app 也能用
        from app.auth.service import AuthService

        service = AuthService()
    return service


async def current_user(request: Request) -> dict[str, Any]:
    """当前登录用户（必需）；未登录抛 1200。"""
    token = _bearer_token(request)
    if not token:
        raise BizError(UNAUTHORIZED, "未登录：请携带 Authorization: Bearer <access_token>")
    return await _resolve_service(request).authenticate(token)


async def optional_user(request: Request) -> dict[str, Any] | None:
    """当前登录用户（可选）；未登录/令牌失效返回 None（用于公开+个性化接口）。"""
    token = _bearer_token(request)
    if not token:
        return None
    try:
        return await _resolve_service(request).authenticate(token)
    except BizError as exc:
        logger.debug("可选认证跳过: {}", exc.msg)
        return None
