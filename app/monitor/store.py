"""监控存储层：PostgreSQL 表结构与 CRUD。

管理三类数据：
  - ``monitor_components``   监控组件配置（web 监控页动态添加，持久化到 PG）
  - ``monitor_field_meanings`` 各业务名称（page）下 Ext 槽位的含义（用户在监控平台配置）
  - ``user_token_usage``     用户累计 token 消耗（按模型区分，含费用）

依赖 config.yaml ``database``（backend=postgres）；非 postgres 后端调用会抛出
``MonitorStoreError``（监控功能需 PostgreSQL）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

__all__ = [
    "MonitorStoreError",
    "is_available",
    "ensure_tables",
    "aclose",
    # 组件配置
    "list_components",
    "create_component",
    "update_component",
    "delete_component",
    # 字段含义
    "get_field_meanings",
    "upsert_field_meanings",
    # 用户 token 汇总
    "upsert_user_token_usage",
    "get_user_token_usage",
]


class MonitorStoreError(RuntimeError):
    """监控存储不可用（未配置 postgres 等）。"""


_pool: AsyncConnectionPool | None = None

#: 建表语句（逐条执行；连接池 prepare_threshold=0，多语句无法用 prepared statement）
_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS monitor_components (
        id          SERIAL PRIMARY KEY,
        name        TEXT NOT NULL,
        page        TEXT NOT NULL,
        metric      TEXT NOT NULL DEFAULT 'p0',
        model       TEXT,
        stat        TEXT NOT NULL DEFAULT 'sum',
        granularity TEXT NOT NULL DEFAULT 'minute',
        range_type  TEXT NOT NULL DEFAULT '30',
        start_time  TIMESTAMPTZ,
        end_time    TIMESTAMPTZ,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS monitor_field_meanings (
        page        TEXT NOT NULL,
        slot        TEXT NOT NULL,
        label       TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (page, slot)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_token_usage (
        user_id       TEXT NOT NULL,
        model         TEXT NOT NULL,
        input_tokens  BIGINT NOT NULL DEFAULT 0,
        output_tokens BIGINT NOT NULL DEFAULT 0,
        total_tokens  BIGINT NOT NULL DEFAULT 0,
        cost          NUMERIC(14,6) NOT NULL DEFAULT 0,
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, model)
    )
    """,
]


def _backend_is_postgres() -> bool:
    from app.config import get_app_config

    return get_app_config().database.backend == "postgres"


def is_available() -> bool:
    """监控存储是否可用（仅 postgres 后端支持）。"""
    return _backend_is_postgres()


async def _get_pool() -> AsyncConnectionPool:
    """惰性创建并打开连接池（首次使用建表）。"""
    global _pool
    if _pool is not None:
        return _pool
    if not _backend_is_postgres():
        raise MonitorStoreError("监控存储需要 PostgreSQL（config.yaml database.backend=postgres）")

    from app.config import get_app_config
    from app.core.checkpointer import _build_postgres_pool

    url = get_app_config().database.postgres_url
    if not url:
        raise MonitorStoreError("database.backend=postgres 但未配置 database.postgres_url")
    _pool = _build_postgres_pool(url)
    # 限制连接/建表耗时，避免 PG 不可达时拖垮请求路径
    await asyncio.wait_for(_pool.open(), timeout=10.0)
    await asyncio.wait_for(ensure_tables(), timeout=10.0)
    return _pool


async def ensure_tables() -> None:
    """创建监控相关表（幂等，逐条执行）。"""
    pool = _pool
    if pool is None:
        return
    async with pool.connection() as conn:
        for statement in _SCHEMA_STATEMENTS:
            await conn.execute(statement)


async def aclose() -> None:
    """关闭连接池（应用 shutdown 时调用）。"""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# ---------------------------------------------------------------------------
# 监控组件配置
# ---------------------------------------------------------------------------


async def list_components() -> list[dict]:
    """列出全部监控组件（按创建时间排序）。"""
    pool = await _get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, name, page, metric, model, stat, granularity, range_type, "
            "to_char(start_time, 'YYYY-MM-DD\"T\"HH24:MI:SS') AS start_time, "
            "to_char(end_time, 'YYYY-MM-DD\"T\"HH24:MI:SS') AS end_time, "
            "to_char(created_at, 'YYYY-MM-DD\"T\"HH24:MI:SS') AS created_at, "
            "to_char(updated_at, 'YYYY-MM-DD\"T\"HH24:MI:SS') AS updated_at "
            "FROM monitor_components ORDER BY id"
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def create_component(
    *,
    name: str,
    page: str,
    metric: str = "p0",
    model: str | None = None,
    stat: str = "sum",
    granularity: str = "minute",
    range_type: str = "30",
    start_time: str | None = None,
    end_time: str | None = None,
) -> int:
    """创建监控组件，返回新 id。"""
    pool = await _get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO monitor_components (name, page, metric, model, stat, granularity, range_type, start_time, end_time) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz) RETURNING id",
            (name, page, metric, model or None, stat, granularity, range_type, start_time or None, end_time or None),
        )
        row = await cur.fetchone()
    return int(row["id"])


async def update_component(component_id: int, **fields: Any) -> bool:
    """更新监控组件（只更新传入字段），返回是否存在。"""
    allowed = {"name", "page", "metric", "model", "stat", "granularity", "range_type", "start_time", "end_time"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return True
    # start_time / end_time 显式转 timestamptz，其余字段直接绑定
    col_expr: list[str] = []
    params: list[Any] = []
    for key, value in updates.items():
        col_expr.append(f"{key} = %s::timestamptz" if key in ("start_time", "end_time") else f"{key} = %s")
        params.append(value)
    params.append(component_id)
    pool = await _get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            f"UPDATE monitor_components SET {', '.join(col_expr)}, updated_at = now() WHERE id = %s",
            params,
        )
        return cur.rowcount > 0


async def delete_component(component_id: int) -> bool:
    """删除监控组件，返回是否存在。"""
    pool = await _get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute("DELETE FROM monitor_components WHERE id = %s", (component_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Ext 槽位含义（按 page 配置）
# ---------------------------------------------------------------------------


async def get_field_meanings(page: str) -> list[dict]:
    """取某业务名称下 P0-P14 的含义配置。"""
    pool = await _get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT slot, label, description FROM monitor_field_meanings WHERE page = %s ORDER BY slot",
            (page,),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def upsert_field_meanings(page: str, meanings: list[dict]) -> None:
    """批量保存某 page 的槽位含义（label/description）。"""
    pool = await _get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            for m in meanings:
                await cur.execute(
                    "INSERT INTO monitor_field_meanings (page, slot, label, description) VALUES (%s, %s, %s, %s) ON CONFLICT (page, slot) DO UPDATE SET label = EXCLUDED.label, description = EXCLUDED.description",
                    (page, str(m.get("slot", "")), str(m.get("label", "") or ""), str(m.get("description", "") or "")),
                )


# ---------------------------------------------------------------------------
# 用户 token 汇总（按模型区分）
# ---------------------------------------------------------------------------


async def upsert_user_token_usage(
    *,
    user_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    cost: float = 0.0,
) -> None:
    """累计某用户某模型的 token 消耗（原子增量）。"""
    pool = await _get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO user_token_usage (user_id, model, input_tokens, output_tokens, total_tokens, cost) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (user_id, model) DO UPDATE SET "
            "  input_tokens = user_token_usage.input_tokens + EXCLUDED.input_tokens, "
            "  output_tokens = user_token_usage.output_tokens + EXCLUDED.output_tokens, "
            "  total_tokens = user_token_usage.total_tokens + EXCLUDED.total_tokens, "
            "  cost = user_token_usage.cost + EXCLUDED.cost, "
            "  updated_at = now()",
            (user_id, model, int(input_tokens), int(output_tokens), int(total_tokens), float(cost)),
        )


async def get_user_token_usage(user_id: str) -> list[dict]:
    """取某用户各模型的累计 token 消耗。"""
    pool = await _get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT user_id, model, input_tokens, output_tokens, total_tokens, cost, to_char(updated_at, 'YYYY-MM-DD\"T\"HH24:MI:SS') AS updated_at FROM user_token_usage WHERE user_id = %s ORDER BY total_tokens DESC",
            (user_id,),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]
