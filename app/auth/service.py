"""认证业务编排：注册 / 登录 / 刷新（旋转）/ 登出 / 当前用户。

令牌策略：
  - access：短时效（``auth.access_ttl_minutes``）、无状态校验，前端内存持有；
  - refresh：长时效（``auth.refresh_ttl_days``）、**落库**（``user_tokens``），
    刷新即旋转（旧 jti 立即撤销），登录设备可枚举/可下线。

安全细则：
  - 登录失败与用户不存在返回同一错误码（不泄露账号是否存在）；
  - 密码校验用恒定时间比较（见 :mod:`app.auth.password`）；
  - 命中旧参数的哈希时，登录成功后透明升级（rehash）。
"""

from __future__ import annotations

from typing import Any

from app.auth import password as pwd
from app.auth import tokens
from app.config import get_app_config
from app.core.log import logger
from app.core.response import CREDENTIALS_INVALID, PASSWORD_TOO_WEAK, STORE_UNAVAILABLE, TOKEN_INVALID, USER_DISABLED, USER_EXISTS, BizError
from app.session import store

__all__ = ["AuthService"]

INVALID_CREDENTIALS_MSG = "账号或密码错误"


class AuthService:
    """账号密码 + JWT 认证服务（无状态，可全局单例复用）。"""

    def __init__(self) -> None:
        self._config = get_app_config().auth

    # ------------------------------------------------------------------ 能力探测

    @property
    def available(self) -> bool:
        """认证是否可用（开关打开 + 密钥已配置 + 业务库已配置）。"""
        return self._config.usable and store.is_available()

    def _require_available(self) -> None:
        if not self._config.usable:
            raise BizError(STORE_UNAVAILABLE, "认证未启用：请配置 config.yaml auth.jwt_secret / .env JWT_SECRET")
        if not store.is_available():
            raise BizError(STORE_UNAVAILABLE, "认证依赖业务库：请配置 business_database.postgres_url / .env BUSINESS_DATABASE_URL")

    # ------------------------------------------------------------------ 注册 / 登录

    async def register(
        self,
        *,
        username: str,
        raw_password: str,
        display_name: str = "",
        email: str | None = None,
    ) -> dict[str, Any]:
        """注册新用户；首个注册用户自动获得 ``admin`` 角色（便于引导运维）。"""
        self._require_available()
        username = (username or "").strip()
        if len(username) < 3:
            raise BizError(CREDENTIALS_INVALID, "用户名至少 3 个字符")
        self._check_password_strength(raw_password)

        roles = ["user"]
        try:
            if await store.count_users() == 0:
                roles = ["user", "admin"]
        except Exception as exc:
            logger.warning("统计用户数失败（按普通用户注册）: {}", exc)

        try:
            user = await store.create_user(
                username=username,
                password_hash=pwd.hash_password(raw_password),
                display_name=(display_name or "").strip() or username,
                email=(email or "").strip() or None,
                roles=roles,
            )
        except store.StoreError as exc:
            raise BizError(USER_EXISTS, str(exc)) from exc
        logger.info("注册用户: {} ({})", user["username"], user["id"])
        return _public_user(user)

    async def login(
        self,
        *,
        username: str,
        raw_password: str,
        user_agent: str = "",
        client_ip: str | None = None,
    ) -> dict[str, Any]:
        """账号密码登录，返回 access/refresh 令牌与用户信息。"""
        self._require_available()
        user = await store.get_user_by_username(username or "")
        if user is None or not pwd.verify_password(raw_password, user.get("password_hash") or ""):
            raise BizError(CREDENTIALS_INVALID, INVALID_CREDENTIALS_MSG)
        if str(user.get("status")) != "active":
            raise BizError(USER_DISABLED, "账号已被禁用，请联系管理员")

        # 透明升级：旧参数哈希在登录成功后按当前参数重写
        if pwd.needs_rehash(user.get("password_hash") or ""):
            try:
                await store.update_password_hash(str(user["id"]), pwd.hash_password(raw_password))
                logger.info("密码哈希已升级到当前参数: user={}", user["id"])
            except Exception as exc:
                logger.warning("密码哈希升级失败（不影响登录）: {}", exc)

        token_pair = await self._issue_tokens(user, user_agent=user_agent, client_ip=client_ip)
        try:
            await store.touch_last_login(str(user["id"]))
        except Exception as exc:
            logger.warning("刷新最近登录时间失败: {}", exc)
        return {"user": _public_user(user), **token_pair}

    async def refresh(self, *, refresh_token: str, user_agent: str = "", client_ip: str | None = None) -> dict[str, Any]:
        """刷新令牌（旋转：旧 refresh 立即撤销，签发新的一对）。"""
        self._require_available()
        payload = self.decode(refresh_token, expected_type=tokens.REFRESH)
        record = await store.get_refresh_token(payload.jti)
        if record is None:
            raise BizError(TOKEN_INVALID, "令牌不存在或已失效")
        if record.get("revoked_at") is not None:
            raise BizError(TOKEN_INVALID, "令牌已撤销，请重新登录")
        if record.get("token_hash") != tokens.hash_token(refresh_token):
            raise BizError(TOKEN_INVALID, "令牌校验失败，请重新登录")

        user = await store.get_user_by_id(str(record["user_id"]))
        if user is None or str(user.get("status")) != "active":
            raise BizError(USER_DISABLED, "账号不可用，请重新登录")

        await store.revoke_refresh_token(payload.jti)
        token_pair = await self._issue_tokens(user, user_agent=user_agent, client_ip=client_ip)
        return {"user": _public_user(user), **token_pair}

    async def logout(self, *, refresh_token: str = "", user_id: str = "", all_devices: bool = False) -> dict[str, Any]:
        """登出：撤销当前 refresh（``all_devices=True`` 时撤销该用户全部令牌）。"""
        self._require_available()
        revoked = 0
        if all_devices and user_id:
            revoked = await store.revoke_user_tokens(user_id)
        elif refresh_token:
            try:
                payload = self.decode(refresh_token, expected_type=tokens.REFRESH)
            except BizError:
                # 登出对"令牌已失效"保持幂等：无需报错
                return {"revoked": 0}
            revoked = 1 if await store.revoke_refresh_token(payload.jti) else 0
        return {"revoked": revoked}

    async def change_password(self, *, user_id: str, old_password: str, new_password: str) -> dict[str, Any]:
        """改密：校验旧密码 → 写新哈希 → **撤销该用户全部 refresh token**（强制重新登录）。

        已签发的 access token 在有效期内仍可用（短时效），这是"无状态校验"的固有取舍；
        需要立即失效时可调 ``revoke_user_tokens`` 并配合黑名单（当前未引入）。
        """
        self._require_available()
        user = await store.get_user_by_id(user_id)
        if user is None:
            raise BizError(CREDENTIALS_INVALID, INVALID_CREDENTIALS_MSG)
        if not pwd.verify_password(old_password, user.get("password_hash") or ""):
            raise BizError(CREDENTIALS_INVALID, "原密码不正确")
        self._check_password_strength(new_password)

        await store.update_password_hash(user_id, pwd.hash_password(new_password))
        revoked = await store.revoke_user_tokens(user_id)
        logger.info("用户改密成功: user={} 撤销令牌数={}", user_id, revoked)
        return {"revoked_tokens": revoked}

    # ------------------------------------------------------------------ 校验

    def decode(self, token: str, *, expected_type: str = tokens.ACCESS) -> tokens.TokenPayload:
        """校验令牌（签名/有效期/类型），失败转业务异常。"""
        from app.core.response import TOKEN_EXPIRED

        try:
            return tokens.decode_token(
                token,
                secret=self._config.jwt_secret,
                algorithm=self._config.jwt_algorithm,
                issuer=self._config.issuer,
                expected_type=expected_type,
            )
        except tokens.TokenError as exc:
            raise BizError(TOKEN_EXPIRED if exc.expired else TOKEN_INVALID, str(exc)) from exc

    async def authenticate(self, token: str) -> dict[str, Any]:
        """用 access token 解析当前用户（含状态校验）。"""
        self._require_available()
        payload = self.decode(token, expected_type=tokens.ACCESS)
        user = await store.get_user_by_id(payload.sub)
        if user is None:
            raise BizError(TOKEN_INVALID, "用户不存在或已注销")
        if str(user.get("status")) != "active":
            raise BizError(USER_DISABLED, "账号已被禁用")
        return _public_user(user)

    # ------------------------------------------------------------------ 内部

    def _check_password_strength(self, raw_password: str) -> None:
        min_len = self._config.password_min_length
        if not raw_password or len(raw_password) < min_len:
            raise BizError(PASSWORD_TOO_WEAK, f"密码至少 {min_len} 个字符")
        kinds = sum(
            [
                any(c.islower() for c in raw_password),
                any(c.isupper() for c in raw_password),
                any(c.isdigit() for c in raw_password),
                any(not c.isalnum() for c in raw_password),
            ]
        )
        if kinds < 2:
            raise BizError(PASSWORD_TOO_WEAK, "密码需包含字母、数字、符号中的至少两类")

    async def _issue_tokens(self, user: dict[str, Any], *, user_agent: str, client_ip: str | None) -> dict[str, Any]:
        """签发 access + refresh，并把 refresh 元数据落库。"""
        user_id = str(user["id"])
        roles = list(user.get("roles") or [])
        access = tokens.create_access_token(
            user_id,
            secret=self._config.jwt_secret,
            algorithm=self._config.jwt_algorithm,
            issuer=self._config.issuer,
            ttl_minutes=self._config.access_ttl_minutes,
            roles=roles,
        )
        refresh, jti, exp = tokens.create_refresh_token(
            user_id,
            secret=self._config.jwt_secret,
            algorithm=self._config.jwt_algorithm,
            issuer=self._config.issuer,
            ttl_days=self._config.refresh_ttl_days,
        )
        await store.store_refresh_token(
            user_id=user_id,
            jti=jti,
            token_hash=tokens.hash_token(refresh),
            expires_at_epoch=exp,
            user_agent=user_agent,
            client_ip=client_ip,
        )
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_in": self._config.access_ttl_minutes * 60,
        }


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    """用户行 → 对外字段（**绝不包含 password_hash**）。"""
    return {
        "user_id": str(user.get("id")),
        "username": user.get("username", ""),
        "display_name": user.get("display_name", ""),
        "email": user.get("email"),
        "roles": list(user.get("roles") or []),
    }
