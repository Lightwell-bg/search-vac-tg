"""Print usage statistics from the database. Usage: python scripts/stats.py"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio  # noqa: E402

from src.bot.stats_format import format_stats  # noqa: E402
from src.config import load_settings  # noqa: E402
from src.db.database import Database  # noqa: E402
from src.db.repository import Repository  # noqa: E402


async def main() -> None:
    settings = load_settings()
    db = Database(settings.database_url)
    await db.init()
    try:
        print(format_stats(await Repository(db).get_stats()))
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
