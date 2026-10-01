"""Async SQLite engine and session factory."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.db.models import Base


# (table, column, DDL) added after the first release; create_all never alters existing tables
_ADDED_COLUMNS = (
    ("channels", "enabled", "BOOLEAN NOT NULL DEFAULT 1"),
    ("channels", "click_callbacks", "BOOLEAN NOT NULL DEFAULT 1"),
)


# (index name, table, column): created with IF NOT EXISTS because create_all skips existing tables
_ADDED_INDEXES = (
    ("ix_messages_received_at", "messages", "received_at"),
    ("ix_messages_received_id", "messages", "received_at, id"),
)


def _migrate(conn) -> None:  # noqa: ANN001
    """Lightweight in-place migration: add missing columns (idempotent, keeps all rows)."""
    if conn.dialect.name != "sqlite":
        return
    for table, column, ddl in _ADDED_COLUMNS:
        cols = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
        if cols and column not in cols:
            conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    for name, table, column in _ADDED_INDEXES:
        conn.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({column})")


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self.engine = create_async_engine(url)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine.sync_engine, "connect")
            def _pragmas(dbapi_conn, _record):  # noqa: ANN001
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=5000")
                cur.close()
        self._sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_migrate)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessionmaker() as s:
            yield s

    async def close(self) -> None:
        await self.engine.dispose()
