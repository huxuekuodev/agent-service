"""认证单元测试（离线：密码哈希 + JWT 签发/校验 + 认证配置解析）。"""

from __future__ import annotations

import time

import pytest

from app.auth import password, tokens
from app.auth.tokens import TokenError, decode_token
from app.config import AppConfig

SECRET = "unit-test-secret-0123456789"


# --------------------------------------------------------------------------- 密码


def test_password_roundtrip_and_reject() -> None:
    stored = password.hash_password("Abcd1234!")
    assert stored.startswith("scrypt$")
    assert password.verify_password("Abcd1234!", stored) is True
    assert password.verify_password("Abcd1234", stored) is False
    assert password.verify_password("", stored) is False


def test_password_salt_is_random() -> None:
    assert password.hash_password("same-password") != password.hash_password("same-password")


@pytest.mark.parametrize("stored", ["", "plain", "scrypt$1$2$3", "bcrypt$14$aa$bb$cc$dd", "scrypt$a$b$c$dd$ee"])
def test_password_rejects_malformed_hash(stored: str) -> None:
    assert password.verify_password("whatever", stored) is False


def test_password_needs_rehash_detects_weak_params() -> None:
    weak = password.hash_password("Abcd1234!", n=2**12, r=8, p=1)
    assert password.needs_rehash(weak) is True
    assert password.needs_rehash(password.hash_password("Abcd1234!")) is False
    # 弱参数哈希仍可校验（参数自描述）
    assert password.verify_password("Abcd1234!", weak) is True


# --------------------------------------------------------------------------- JWT


def test_access_token_roundtrip() -> None:
    token = tokens.create_access_token("user-1", secret=SECRET, ttl_minutes=5, roles=["admin"])
    payload = decode_token(token, secret=SECRET, expected_type=tokens.ACCESS)
    assert payload.sub == "user-1"
    assert payload.roles == ["admin"]
    assert payload.exp > int(time.time())


def test_refresh_token_returns_jti_and_expiry() -> None:
    token, jti, exp = tokens.create_refresh_token("user-2", secret=SECRET, ttl_days=1)
    payload = decode_token(token, secret=SECRET, expected_type=tokens.REFRESH)
    assert payload.jti == jti
    assert payload.exp == exp
    assert tokens.hash_token(token) == tokens.hash_token(token)


def test_token_type_cannot_be_confused() -> None:
    access = tokens.create_access_token("user-3", secret=SECRET)
    with pytest.raises(TokenError) as exc:
        decode_token(access, secret=SECRET, expected_type=tokens.REFRESH)
    assert exc.value.reason == "wrong_type"


def test_token_signature_tampering_rejected() -> None:
    token = tokens.create_access_token("user-4", secret=SECRET)
    header, payload, signature = token.split(".")
    forged = f"{header}.{payload}.{signature[:-4]}AAAA"
    with pytest.raises(TokenError) as exc:
        decode_token(forged, secret=SECRET)
    assert exc.value.reason in {"bad_signature", "malformed"}


def test_token_rejects_other_algorithm_and_wrong_secret() -> None:
    token = tokens.create_access_token("user-5", secret=SECRET)
    with pytest.raises(TokenError):
        decode_token(token, secret="another-secret")
    with pytest.raises(TokenError) as exc:
        decode_token(token, secret=SECRET, algorithm="none")
    assert exc.value.reason == "unsupported_alg"


def test_expired_token_reports_expired() -> None:
    now = int(time.time())
    expired = tokens._encode({"sub": "user-6", "typ": tokens.ACCESS, "exp": now - 60}, SECRET, "HS256", "deer-agent")
    with pytest.raises(TokenError) as exc:
        decode_token(expired, secret=SECRET, leeway_seconds=0)
    assert exc.value.expired is True


def test_decode_rejects_garbage() -> None:
    for bad in ("", "a.b", "a.b.c.d", "not-a-token"):
        with pytest.raises(TokenError):
            decode_token(bad, secret=SECRET)


# --------------------------------------------------------------------------- 配置


def test_auth_config_defaults_follow_secret() -> None:
    enabled = AppConfig.from_dict({"models": {"default": "deepseek"}, "auth": {"jwt_secret": SECRET, "access_ttl_minutes": 30}})
    assert enabled.auth.usable is True
    assert enabled.auth.access_ttl_minutes == 30
    assert enabled.auth.jwt_algorithm == "HS256"

    disabled = AppConfig.from_dict({"models": {"default": "deepseek"}, "auth": {}})
    assert disabled.auth.usable is False  # 无密钥 → 认证不可用


def test_business_database_config() -> None:
    config = AppConfig.from_dict(
        {
            "models": {"default": "deepseek"},
            "business_database": {"postgres_url": "postgresql://u:p@h:5432/db", "sessions_page_size": 5, "fail_fast": True},
        }
    )
    assert config.business_database.enabled is True
    assert config.business_database.sessions_page_size == 5
    assert config.business_database.fail_fast is True
    # 未配置时按"不可用"处理（接口降级而非 500）
    assert AppConfig.from_dict({"models": {"default": "deepseek"}}).business_database.enabled is False
