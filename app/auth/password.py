"""密码哈希：标准库 ``hashlib.scrypt``（RFC 7914，内存硬 KDF）。

存储格式（单字段自描述，便于日后换参数/换算法平滑升级）::

    scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>

默认参数 ``n=2**14, r=8, p=1``（OWASP 建议下限，约 16MB 内存 / 单次 ~50ms）；
校验时按存储里的参数重算，因此调参不会让旧密码失效；:func:`needs_rehash`
用于登录成功后判断是否需要用新参数重写哈希。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

__all__ = ["hash_password", "needs_rehash", "verify_password"]

_ALGO = "scrypt"
_DEFAULT_N = 2**14
_DEFAULT_R = 8
_DEFAULT_P = 1
_SALT_BYTES = 16
_KEY_LEN = 32
#: scrypt 内存上限（Python 默认 32MB；n=2**14,r=8 需 ~16MB，显式放宽避免误报 error）
_MAXMEM = 64 * 1024 * 1024


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_KEY_LEN, maxmem=_MAXMEM)


def hash_password(password: str, *, n: int = _DEFAULT_N, r: int = _DEFAULT_R, p: int = _DEFAULT_P) -> str:
    """生成密码哈希（每次调用使用新随机盐）。"""
    if not password:
        raise ValueError("密码不能为空")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _derive(password, salt, n, r, p)
    return f"{_ALGO}${n}${r}${p}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码（恒定时间比较）；``stored`` 非法/为空时返回 False。"""
    if not password or not stored:
        return False
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != _ALGO:
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = _b64d(parts[4])
        expected = _b64d(parts[5])
    except (ValueError, TypeError):
        return False
    try:
        actual = _derive(password, salt, n, r, p)
    except ValueError:
        # 参数非法（如 n 非 2 的幂）视作校验失败，不抛给调用方
        return False
    return hmac.compare_digest(actual, expected)


def needs_rehash(stored: str, *, n: int = _DEFAULT_N, r: int = _DEFAULT_R, p: int = _DEFAULT_P) -> bool:
    """存储的哈希是否弱于当前默认参数（登录成功后据此透明升级）。"""
    parts = (stored or "").split("$")
    if len(parts) != 6 or parts[0] != _ALGO:
        return True
    try:
        return (int(parts[1]), int(parts[2]), int(parts[3])) != (n, r, p)
    except ValueError:
        return True
