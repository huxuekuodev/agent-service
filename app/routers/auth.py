"""认证接口：注册 / 登录 / 刷新 / 登出 / 当前用户。

统一信封 ``{data, msg, status}``（HTTP 始终 200）：
  - 1200 未登录、1201 令牌过期（前端应静默刷新）、1202 令牌非法（重新登录）
  - 1203 用户名已占用、1204 密码强度不足、1206 账号或密码错误、1207 存储不可用
"""

from __future__ import annotations

import ipaddress
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.auth.deps import current_user
from app.auth.service import AuthService
from app.core.response import ok

router = APIRouter(prefix="/auth", tags=["auth"])


def get_auth_service(request: Request) -> AuthService:
    """从 app.state 获取 AuthService（lifespan 注入；缺失时惰性构造）。"""
    service = getattr(request.app.state, "auth_service", None)
    if service is None:
        service = AuthService()
        request.app.state.auth_service = service
    return service


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64, description="登录名（忽略大小写唯一）")
    password: str = Field(..., description="明文密码，服务端只存 scrypt 哈希")
    display_name: str = Field(default="", max_length=64, description="展示名，缺省用用户名")
    email: str | None = Field(default=None, max_length=128)


class LoginRequest(BaseModel):
    username: str = Field(..., description="登录名")
    password: str = Field(..., description="明文密码")


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., description="登录/刷新时返回的 refresh_token")


class LogoutRequest(BaseModel):
    refresh_token: str = Field(default="", description="要撤销的 refresh_token（可空）")
    all_devices: bool = Field(default=False, description="True 时撤销该用户全部 refresh token")


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., description="原密码")
    new_password: str = Field(..., description="新密码（需满足强度要求）")


def _client_ip(request: Request) -> str | None:
    """取客户端 IP（优先反向代理的 ``X-Forwarded-For`` 首段）。

    非 IP 值（如 ASGI 测试客户端的 ``testclient``）返回 None：``user_tokens.client_ip``
    是 ``inet`` 类型，脏值会让登录直接失败，因此这里做严格校验。
    """
    forwarded = request.headers.get("x-forwarded-for") or ""
    candidate = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "")
    if not candidate:
        return None
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate


@router.post("/register")
async def register(req: RegisterRequest, request: Request, svc: AuthService = Depends(get_auth_service)) -> dict[str, Any]:
    """注册账号（首个用户自动成为管理员）。"""
    user = await svc.register(username=req.username, raw_password=req.password, display_name=req.display_name, email=req.email)
    return ok(user)


@router.post("/login")
async def login(req: LoginRequest, request: Request, svc: AuthService = Depends(get_auth_service)) -> dict[str, Any]:
    """账号密码登录，返回 access_token / refresh_token 与用户信息。"""
    result = await svc.login(
        username=req.username,
        raw_password=req.password,
        user_agent=request.headers.get("user-agent", ""),
        client_ip=_client_ip(request),
    )
    return ok(result)


@router.post("/refresh")
async def refresh(req: RefreshRequest, request: Request, svc: AuthService = Depends(get_auth_service)) -> dict[str, Any]:
    """用 refresh_token 换取新的令牌对（旧 refresh 立即失效：旋转防重放）。"""
    result = await svc.refresh(
        refresh_token=req.refresh_token,
        user_agent=request.headers.get("user-agent", ""),
        client_ip=_client_ip(request),
    )
    return ok(result)


@router.post("/logout")
async def logout(req: LogoutRequest, request: Request, user: dict[str, Any] = Depends(current_user), svc: AuthService = Depends(get_auth_service)) -> dict[str, Any]:
    """登出：撤销当前 refresh token（``all_devices=true`` 时撤销全部设备）。"""
    result = await svc.logout(refresh_token=req.refresh_token, user_id=str(user.get("user_id") or ""), all_devices=req.all_devices)
    return ok(result)


@router.get("/me")
async def me(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """当前登录用户信息（前端启动时校验登录态）。"""
    return ok(user)


@router.post("/password")
async def change_password(req: ChangePasswordRequest, user: dict[str, Any] = Depends(current_user), svc: AuthService = Depends(get_auth_service)) -> dict[str, Any]:
    """改密：校验旧密码后写新哈希，并撤销该用户全部 refresh token（强制重新登录）。"""
    result = await svc.change_password(user_id=str(user.get("user_id") or ""), old_password=req.old_password, new_password=req.new_password)
    return ok(result)
