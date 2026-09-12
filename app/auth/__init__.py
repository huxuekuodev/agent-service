"""认证（账号密码 + JWT）。

模块划分：
  - :mod:`app.auth.password` 密码哈希（scrypt，标准库实现，无额外依赖）
  - :mod:`app.auth.tokens`   JWT 签发/校验（HS256，标准库实现）
  - :mod:`app.auth.service`  认证业务编排（注册/登录/刷新/登出）
  - :mod:`app.auth.deps`     FastAPI 依赖：``current_user`` / ``optional_user``

设计约束：
  - 只接受 ``HS256``（显式算法白名单，杜绝 alg 混淆 / alg=none）；
  - refresh token **落库**（``user_tokens``：jti + SHA-256 哈希），刷新即旋转；
  - 密码只存哈希（``scrypt$n$r$p$salt$hash``），校验用恒定时间比较。
"""

from __future__ import annotations

from app.auth.deps import current_user, optional_user
from app.auth.password import hash_password, needs_rehash, verify_password
from app.auth.service import AuthService
from app.auth.tokens import TokenError, TokenPayload, create_access_token, create_refresh_token, decode_token, hash_token

__all__ = [
    "AuthService",
    "TokenError",
    "TokenPayload",
    "create_access_token",
    "create_refresh_token",
    "current_user",
    "decode_token",
    "hash_password",
    "hash_token",
    "needs_rehash",
    "optional_user",
    "verify_password",
]
