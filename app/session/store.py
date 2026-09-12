"""业务库存储层：用户 / 令牌 / 会话 / 消息（PostgreSQL）。

与 :mod:`app.monitor.store` 同模式（模块级异步连接池 + 惰性建池），但连的是
**业务库** ``config.business_database.postgres_url``（与 checkpointer 库分离）。

表结构由 ``deploy/sql/business_schema.sql`` 创建（本模块只做**存在性校验**，
不在运行时建表：生产环境 DDL 走迁移脚本，避免应用账号持有 DDL 权限）。

统一约定：
  - 全部函数 async，返回 dict（psycopg ``dict_row``）；
  - 会话/消息查询一律带 ``user_id`` 条件（越权即"不存在"）；
  - 逻辑删除：查询追加 ``deleted_at IS NULL``，删除只置 ``deleted_at``；
  - 消息写入统一走 ``append_message()`` 存储过程（幂等 + seq 分配 + 会话计数）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from psycopg_pool import AsyncConnectionPool

from app.core.log import logger

__all__ = [
    "StoreError",
    "is_available",
    "get_pool",
    "aclose",
    "ensure_ready",
    # 用户
    "create_user",
    "get_user_by_id",
    "get_user_by_username",
    "count_users",
    "touch_last_login",
    "update_password_hash",
    # 令牌
    "store_refresh_token",
    "get_refresh_token",
    "revoke_refresh_token",
    "revoke_user_tokens",
    "purge_expired_tokens",
    # 会话
    "create_session",
    "get_session",
    "list_sessions",
    "update_session",
    "soft_delete_session",
    # 消息
    "append_message",
    "list_messages",
]


class StoreError(RuntimeError):
    """业务库不可用（未配置 / 连接失败 / 表未建）。"""


_pool: AsyncConnectionPool | None = None

#: 业务库必需的表（缺表说明 DDL 未执行，直接给出明确指引）
_REQUIRED_TABLES = ("users", "user_tokens", "sessions", "messages")


def _business_config():
    from app.config import get_app_config

    return get_app_config().business_database


def is_available() -> bool:
    """业务库是否已配置（未配置时接口应降级为"存储不可用"而非 500）。"""
    return _business_config().enabled


async def get_pool() -> AsyncConnectionPool:
    """惰性创建并打开业务库连接池（首次调用校验表结构）。"""
    global _pool
    if _pool is not None:
        return _pool
    if not is_available():
        raise StoreError("未配置业务库：请在 config.yaml business_database.postgres_url 或 .env BUSINESS_DATABASE_URL 中配置")

    from app.core.checkpointer import _build_postgres_pool

    _pool = _build_postgres_pool(_business_config().postgres_url)
    # 限制连接/校验耗时，避免 PG 不可达时拖垮请求路径
    await asyncio.wait_for(_pool.open(), timeout=10.0)
    await asyncio.wait_for(ensure_ready(), timeout=10.0)
    return _pool


async def ensure_ready() -> None:
    """校验必需表存在（DDL 未执行时抛出带指引的错误）。"""
    pool = _pool
    if pool is None:
        return
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (f"public.{_REQUIRED_TABLES[0]}",))
        row = await cur.fetchone()
        if not row or not row["ok"]:
            raise StoreError("业务库缺少表：请先执行 psql -d <business_db> -f deploy/sql/business_schema.sql")


async def aclose() -> None:
    """关闭连接池（应用 shutdown 时调用）。"""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def _fetchone(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchone()


async def _fetchall(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return list(await cur.fetchall())


# ---------------------------------------------------------------------------
# 用户
# ---------------------------------------------------------------------------


async def create_user(
    *,
    username: str,
    password_hash: str,
    display_name: str = "",
    email: str | None = None,
    roles: list[str] | None = None,
) -> dict[str, Any]:
    """创建用户；用户名（忽略大小写）冲突时抛 :class:`StoreError`。"""
    from psycopg.errors import UniqueViolation

    pool = await get_pool()
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO users (username, display_name, email, password_hash, roles) VALUES (%s, %s, %s, %s, %s) RETURNING id, username, display_name, roles, created_at",
                (username, display_name or username, email or None, password_hash, list(roles or ["user"])),
            )
            row = await cur.fetchone()
    except UniqueViolation as exc:
        raise StoreError("用户名已被占用") from exc
    # 本地账号身份映射（预留第三方登录扩展；同一事务语义由 UNIQUE 约束保证）
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO user_identities (user_id, provider, provider_uid) VALUES (%s, 'local', %s) ON CONFLICT DO NOTHING",
            (row["id"], username.lower()),
        )
    return dict(row)


async def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    """按 id 取用户（含 password_hash，供改密/校验用）。"""
    return await _fetchone(
        "SELECT id, username, display_name, email, password_hash, status, roles, last_login_at FROM users WHERE id = %s AND deleted_at IS NULL",
        (user_id,),
    )


async def get_user_by_username(username: str) -> dict[str, Any] | None:
    """按用户名（忽略大小写）取用户。"""
    return await _fetchone(
        "SELECT id, username, display_name, email, password_hash, status, roles, last_login_at FROM users WHERE lower(username) = lower(%s) AND deleted_at IS NULL",
        (username,),
    )


async def count_users() -> int:
    """用户总数（用于"首个注册用户即管理员"的引导逻辑）。"""
    row = await _fetchone("SELECT count(*) AS n FROM users WHERE deleted_at IS NULL")
    return int(row["n"]) if row else 0


async def touch_last_login(user_id: str) -> None:
    """刷新最近登录时间。"""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (user_id,))


async def update_password_hash(user_id: str, password_hash: str) -> None:
    """更新密码哈希（改密 / 透明升级哈希参数）。"""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, user_id))


# ---------------------------------------------------------------------------
# JWT 令牌（refresh 落库：可撤销、可旋转、可列出设备）
# ---------------------------------------------------------------------------


async def store_refresh_token(
    *,
    user_id: str,
    jti: str,
    token_hash: str,
    expires_at_epoch: int,
    user_agent: str = "",
    client_ip: str | None = None,
) -> None:
    """保存 refresh token 元数据（不存明文）。"""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO user_tokens (user_id, jti, token_hash, token_type, expires_at, user_agent, client_ip) VALUES (%s, %s, %s, 'refresh', to_timestamp(%s), %s, %s::inet) ON CONFLICT (jti) DO NOTHING",
            (user_id, jti, token_hash, expires_at_epoch, user_agent or None, client_ip or None),
        )


async def get_refresh_token(jti: str) -> dict[str, Any] | None:
    """取 refresh token 记录（校验撤销/过期由调用方判断）。"""
    return await _fetchone(
        "SELECT id, user_id, jti, token_hash, expires_at, revoked_at FROM user_tokens WHERE jti = %s",
        (jti,),
    )


async def revoke_refresh_token(jti: str) -> bool:
    """撤销单个 refresh token（登出 / 旋转旧令牌）。"""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute("UPDATE user_tokens SET revoked_at = now() WHERE jti = %s AND revoked_at IS NULL", (jti,))
        return cur.rowcount > 0


async def revoke_user_tokens(user_id: str) -> int:
    """撤销该用户全部 refresh token（改密 / 一键下线所有设备）。"""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute("UPDATE user_tokens SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (user_id,))
        return int(cur.rowcount)


async def purge_expired_tokens(older_than_days: int = 7) -> int:
    """物理清理已过期令牌（供后台任务调用）。"""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute("DELETE FROM user_tokens WHERE expires_at < now() - make_interval(days => %s)", (older_than_days,))
        return int(cur.rowcount)


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------

_SESSION_COLUMNS = "id, user_id, title, status, model_role, checkpoint_thread_id, message_count, last_preview, meta, created_at, updated_at, last_message_at"


def _session_dict(row: dict[str, Any]) -> dict[str, Any]:
    """会话行 → 对外 dict（统一 ISO 时间与字段名）。"""
    out = dict(row)
    out["session_id"] = str(out.pop("id"))
    out["thread_id"] = out.get("checkpoint_thread_id") or out["session_id"]
    for key in ("created_at", "updated_at", "last_message_at"):
        value = out.get(key)
        out[key] = value.isoformat() if isinstance(value, datetime) else value
    return out


async def create_session(
    *,
    user_id: str,
    session_id: str,
    title: str = "",
    model_role: str | None = None,
) -> dict[str, Any]:
    """创建会话（``checkpoint_thread_id`` 默认等于会话 id）。"""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"INSERT INTO sessions (id, user_id, title, model_role, checkpoint_thread_id) VALUES (%s, %s, %s, %s, %s) RETURNING {_SESSION_COLUMNS}",
            (session_id, user_id, title or "新会话", model_role or None, session_id),
        )
        row = await cur.fetchone()
    return _session_dict(row)


async def get_session(session_id: str, *, user_id: str) -> dict[str, Any] | None:
    """取会话（带归属校验；不存在或不属于该用户都返回 None）。"""
    row = await _fetchone(f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE id = %s AND user_id = %s AND deleted_at IS NULL", (session_id, user_id))
    return _session_dict(row) if row else None


async def list_sessions(
    *,
    user_id: str,
    limit: int = 20,
    cursor: tuple[str, str] | None = None,
    query: str = "",
) -> list[dict[str, Any]]:
    """列出会话（键集分页：``(last_message_at, id)`` 递减）。

    Args:
        cursor: 上一页最后一条的 ``(last_message_at_iso, id)``；首页传 None。
        query: 标题/预览模糊搜索（可选）。
    """
    sql = f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE user_id = %s AND deleted_at IS NULL"
    params: list[Any] = [user_id]
    if query:
        sql += " AND (title ILIKE %s OR last_preview ILIKE %s)"
        params += [f"%{query}%", f"%{query}%"]
    if cursor:
        sql += " AND (coalesce(last_message_at, created_at), id) < (%s::timestamptz, %s::uuid)"
        params += [cursor[0], cursor[1]]
    sql += " ORDER BY coalesce(last_message_at, created_at) DESC, id DESC LIMIT %s"
    params.append(limit)
    rows = await _fetchall(sql, tuple(params))
    return [_session_dict(r) for r in rows]


async def update_session(
    session_id: str,
    *,
    user_id: str,
    title: str | None = None,
    status: str | None = None,
) -> dict[str, Any] | None:
    """重命名 / 归档会话；返回更新后的会话（不存在或不属于该用户返回 None）。"""
    sets: list[str] = []
    params: list[Any] = []
    if title is not None:
        sets.append("title = %s")
        params.append(title.strip()[:200] or "新会话")
    if status is not None:
        sets.append("status = %s")
        params.append(status)
    if not sets:
        return await get_session(session_id, user_id=user_id)

    params += [session_id, user_id]
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"UPDATE sessions SET {', '.join(sets)} WHERE id = %s AND user_id = %s AND deleted_at IS NULL RETURNING {_SESSION_COLUMNS}",
            tuple(params),
        )
        row = await cur.fetchone()
    return _session_dict(row) if row else None


async def soft_delete_session(session_id: str, *, user_id: str) -> dict[str, Any] | None:
    """逻辑删除会话；返回被删会话（供调用方按 ``checkpoint_thread_id`` 清理 checkpoint）。"""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"UPDATE sessions SET deleted_at = now() WHERE id = %s AND user_id = %s AND deleted_at IS NULL RETURNING {_SESSION_COLUMNS}",
            (session_id, user_id),
        )
        row = await cur.fetchone()
    return _session_dict(row) if row else None


# ---------------------------------------------------------------------------
# 消息
# ---------------------------------------------------------------------------


def _message_dict(row: dict[str, Any]) -> dict[str, Any]:
    """消息行 → 对外 dict。"""
    out = dict(row)
    out["message_id"] = out.pop("id", None)
    created = out.get("created_at")
    out["created_at"] = created.isoformat() if isinstance(created, datetime) else created
    return out


async def append_message(
    *,
    session_id: str,
    role: str,
    content: str,
    kind: str = "chat",
    payload: dict[str, Any] | None = None,
    client_msg_id: str | None = None,
    model_role: str | None = None,
    token_input: int | None = None,
    token_output: int | None = None,
    latency_ms: int | None = None,
) -> dict[str, Any]:
    """原子追加消息（分区保障 + 幂等 + seq 分配 + 会话计数），返回消息行。

    幂等：同一 ``client_msg_id`` 重复调用返回既有消息（前端重发不产生重复）。
    """
    import json

    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT * FROM append_message(%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)",
            (
                session_id,
                role,
                kind,
                content or "",
                json.dumps(payload or {}, ensure_ascii=False),
                client_msg_id or None,
                model_role or None,
                token_input,
                token_output,
                latency_ms,
            ),
        )
        row = await cur.fetchone()
    return _message_dict(row)


async def list_messages(
    *,
    session_id: str,
    user_id: str,
    limit: int = 50,
    before_seq: int | None = None,
) -> list[dict[str, Any]]:
    """按 seq 取历史消息（倒序取页、正序返回；只返回该用户自己的会话）。"""
    sql = (
        "SELECT m.id, m.seq, m.role, m.kind, m.content, m.content_type, m.payload, m.model_role, "
        "m.token_input, m.token_output, m.latency_ms, m.created_at "
        "FROM messages m JOIN sessions s ON s.id = m.session_id "
        "WHERE m.session_id = %s AND s.user_id = %s AND s.deleted_at IS NULL AND m.deleted_at IS NULL"
    )
    params: list[Any] = [session_id, user_id]
    if before_seq is not None:
        sql += " AND m.seq < %s"
        params.append(before_seq)
    sql += " ORDER BY m.seq DESC LIMIT %s"
    params.append(limit)

    rows = await _fetchall(sql, tuple(params))
    rows.reverse()
    logger.debug("读取历史消息: session={} 条数={}", session_id, len(rows))
    return [_message_dict(r) for r in rows]
