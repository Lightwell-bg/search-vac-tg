"""Async SQLite engine and session factory."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.db.models import Base


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

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessionmaker() as s:
            yield s

    async def close(self) -> None:
        await self.engine.dispose()
