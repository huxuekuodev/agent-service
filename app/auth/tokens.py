"""JWT 签发与校验（HS256，标准库实现，零额外依赖）。

为什么自己实现：HS256 是 ``base64url(header).base64url(payload).HMAC-SHA256`` 的
确定性构造，标准库即可完整、正确地实现；本项目只使用单一算法 + 显式算法白名单，
因此不存在第三方库要解决的 alg 混淆 / JWKS / RS256 复杂度。

安全要点（实现即约束）：
  - 头里 ``alg`` 固定 ``HS256``，校验时**必须**等于配置算法，拒绝 ``none``/其它算法；
  - 签名比较用 ``hmac.compare_digest``（恒定时间）；
  - 校验 ``exp`` / ``nbf`` / ``iss``，并校验 ``typ``（access / refresh）不串用；
  - refresh token 额外携带 ``jti``，由服务端落库（``user_tokens``）保证可撤销、可旋转。

Claims::

    {"iss": "deer-agent", "sub": "<user_id>", "typ": "access|refresh",
     "jti": "<uuid>", "iat": 1757654321, "exp": 1757657921, "nbf": ..., "roles": [...]}
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ACCESS",
    "REFRESH",
    "TokenError",
    "TokenPayload",
    "create_access_token",
    "create_refresh_token",
    "decode_token",
    "hash_token",
]

ACCESS = "access"
REFRESH = "refresh"

_ALLOWED_ALGS = frozenset({"HS256"})


class TokenError(Exception):
    """令牌非法/过期（``reason`` 便于前端区分：过期可静默刷新）。"""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason

    @property
    def expired(self) -> bool:
        return self.reason == "expired"


@dataclass
class TokenPayload:
    """解析后的令牌载荷。"""

    sub: str
    """用户 id（``users.id``）。"""
    typ: str
    """令牌类型：access / refresh。"""
    jti: str = ""
    exp: int = 0
    iat: int = 0
    roles: list[str] = field(default_factory=list)


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(signing_input: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()


def _encode(claims: dict[str, Any], secret: str, algorithm: str, issuer: str) -> str:
    if algorithm.upper() not in _ALLOWED_ALGS:
        raise TokenError("unsupported_alg", f"仅支持 HS256，当前配置：{algorithm}")
    if not secret:
        raise TokenError("no_secret", "未配置 JWT 密钥（config auth.jwt_secret / .env JWT_SECRET）")

    now = int(time.time())
    payload = {"iss": issuer, "iat": now, "nbf": now}
    payload.update(claims)
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = f"{_b64e(json.dumps(header, separators=(',', ':')).encode())}.{_b64e(json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode())}".encode()
    return f"{signing_input.decode()}.{_b64e(_sign(signing_input, secret))}"


def create_access_token(
    user_id: str,
    *,
    secret: str,
    algorithm: str = "HS256",
    issuer: str = "deer-agent",
    ttl_minutes: int = 120,
    roles: list[str] | None = None,
) -> str:
    """签发 access token（短时效，无状态校验）。"""
    now = int(time.time())
    return _encode(
        {
            "sub": str(user_id),
            "typ": ACCESS,
            "jti": uuid.uuid4().hex,
            "exp": now + max(1, int(ttl_minutes)) * 60,
            "roles": list(roles or []),
        },
        secret,
        algorithm,
        issuer,
    )


def create_refresh_token(
    user_id: str,
    *,
    secret: str,
    algorithm: str = "HS256",
    issuer: str = "deer-agent",
    ttl_days: int = 30,
) -> tuple[str, str, int]:
    """签发 refresh token。

    Returns:
        ``(token, jti, expires_at_epoch)`` —— ``jti`` 与 ``exp`` 用于落库
        （``user_tokens``），从而支持撤销与旋转。
    """
    jti = uuid.uuid4().hex
    now = int(time.time())
    exp = now + max(1, int(ttl_days)) * 86400
    token = _encode(
        {
            "sub": str(user_id),
            "typ": REFRESH,
            "jti": jti,
            "exp": exp,
        },
        secret,
        algorithm,
        issuer,
    )
    return token, jti, exp


def decode_token(
    token: str,
    *,
    secret: str,
    algorithm: str = "HS256",
    issuer: str = "deer-agent",
    expected_type: str | None = None,
    leeway_seconds: int = 5,
) -> TokenPayload:
    """校验并解析令牌；非法时抛 :class:`TokenError`。"""
    if not secret:
        raise TokenError("no_secret", "未配置 JWT 密钥")
    if algorithm.upper() not in _ALLOWED_ALGS:
        raise TokenError("unsupported_alg", f"仅支持 HS256，当前配置：{algorithm}")

    parts = (token or "").split(".")
    if len(parts) != 3:
        raise TokenError("malformed", "令牌格式非法")
    header_b64, payload_b64, signature_b64 = parts

    try:
        header = json.loads(_b64d(header_b64))
        payload = json.loads(_b64d(payload_b64))
        signature = _b64d(signature_b64)
    except Exception as exc:  # 任意解码/解析失败都视为非法令牌
        raise TokenError("malformed", "令牌格式非法") from exc

    if str(header.get("alg", "")).upper() != algorithm.upper():
        raise TokenError("bad_alg", "令牌算法与配置不一致")

    signing_input = f"{header_b64}.{payload_b64}".encode()
    if not hmac.compare_digest(_sign(signing_input, secret), signature):
        raise TokenError("bad_signature", "令牌签名校验失败")

    if issuer and payload.get("iss") != issuer:
        raise TokenError("bad_issuer", "令牌签发方不匹配")

    now = int(time.time())
    try:
        exp = int(payload.get("exp", 0))
        nbf = int(payload.get("nbf", 0))
    except (TypeError, ValueError) as exc:
        raise TokenError("malformed", "令牌时间字段非法") from exc

    if exp and now > exp + leeway_seconds:
        raise TokenError("expired", "令牌已过期")
    if nbf and now + leeway_seconds < nbf:
        raise TokenError("not_yet_valid", "令牌尚未生效")

    sub = str(payload.get("sub") or "")
    typ = str(payload.get("typ") or "")
    if not sub:
        raise TokenError("no_subject", "令牌缺少用户标识")
    if expected_type and typ != expected_type:
        raise TokenError("wrong_type", f"令牌类型不匹配（需要 {expected_type}）")

    return TokenPayload(
        sub=sub,
        typ=typ,
        jti=str(payload.get("jti") or ""),
        exp=exp,
        iat=int(payload.get("iat", 0) or 0),
        roles=list(payload.get("roles") or []),
    )


def hash_token(token: str) -> str:
    """令牌哈希（落库用；库中不存明文 token）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
