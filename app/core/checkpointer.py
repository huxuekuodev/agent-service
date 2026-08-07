"""Checkpointer 工厂（独立服务版）。

根据 config.yaml 的 database 配置创建 checkpointer：
- memory: InMemorySaver（默认，单机开发）
- postgres: AsyncPostgresSaver（集群多节点共享状态）

集群部署必须用 postgres——多节点通过同一个数据库共享会话状态，
否则用户请求发散到不同节点会丢失会话上下文。
"""

from __future__ import annotations

import logging
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)


def create_checkpointer(app_config: Any) -> Any:
    """创建 checkpointer。

    Args:
        app_config: 应用配置（含 database 段）。

    Returns:
        LangGraph checkpointer 实例。
    """
    db = getattr(app_config, "database", None)
    backend = getattr(db, "backend", "memory") if db else "memory"

    if backend == "postgres":
        url = getattr(db, "postgres_url", "") if db else ""
        if not url:
            raise ValueError("database.backend is 'postgres' but database.postgres_url is not set. Set it in config.yaml, e.g. postgresql://user:pass@host:5432/deerflow")
        return _create_postgres_checkpointer(url)

    if backend == "sqlite":
        pass
        # from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        # # 单机多进程可共享 SQLite 文件（注意并发限制）
        # return AsyncSqliteSaver.from_conn_string(":memory:")

    # 默认 memory
    from langgraph.checkpoint.memory import InMemorySaver

    return InMemorySaver()


def _build_postgres_pool(conn_string: str) -> AsyncConnectionPool[AsyncConnection[Any]]:
    """Build an AsyncConnectionPool with TCP keepalive and connection checking."""
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    return AsyncConnectionPool(
        conn_string,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "keepalives": 1,
            "keepalives_idle": 60,
            "keepalives_interval": 10,
            "keepalives_count": 6,
        },
        check=AsyncConnectionPool.check_connection,
    )


def _create_postgres_checkpointer(url: str) -> PostgresCheckpointerHandle:
    """创建 Postgres checkpointer。

    返回 PostgresCheckpointerHandle（async context manager 包装）：
      - 持有 AsyncPostgresSaver + AsyncConnectionPool
      - 由调用方 __aenter__ 进入（打开池 + setup 建表）
      - __aexit__ 释放连接池
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    pool = _build_postgres_pool(url)
    saver = AsyncPostgresSaver(conn=pool)
    return PostgresCheckpointerHandle(saver, pool)


class PostgresCheckpointerHandle:
    """包装 AsyncPostgresSaver + 连接池，提供 async context manager 生命周期。"""

    def __init__(self, saver: Any, pool: Any) -> None:
        self.saver = saver
        self.pool = pool

    async def __aenter__(self) -> Any:
        await self.pool.__aenter__()
        await self.saver.setup()
        return self.saver

    async def __aexit__(self, *exc: Any) -> None:
        await self.pool.__aexit__(*exc)
