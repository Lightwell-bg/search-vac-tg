import pytest_asyncio

from src.db.database import Database
from src.db.repository import Repository


@pytest_asyncio.fixture
async def repo(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}")
    await db.init()
    yield Repository(db)
    await db.close()
