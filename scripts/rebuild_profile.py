"""Rebuild data/profile.json and data/profile.md from materials/.

Usage: python scripts/rebuild_profile.py [--llm] [--dry-run]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
import asyncio  # noqa: E402
import logging  # noqa: E402

from src.config import load_settings  # noqa: E402
from src.profile.builder import build_profile, enrich_with_llm, write_profile  # noqa: E402

log = logging.getLogger("rebuild_profile")


def summary_text(profile: dict) -> str:
    return (
        f"Technologies: {len(profile['technologies'])} "
        f"(from materials: {len(profile.get('technology_documents', {}))})\n"
        f"Portfolio projects: {len(profile['portfolio_projects'])}\n"
        f"Sources read: {len(profile['sources'])}\n"
        f"Provisional: {profile['provisional']}\n"
        f"Top: {', '.join(profile['technologies'][:12])}\n"
        f"Summary: {profile['summary']}"
    )


async def run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings = load_settings()
    profile = build_profile(settings.materials_dir)
    if args.llm:
        from src.db.database import Database
        from src.db.repository import Repository
        from src.llm.openrouter import OpenRouterClient

        db = Database(settings.database_url)
        await db.init()
        llm = OpenRouterClient.from_settings(settings)
        try:
            profile = await enrich_with_llm(profile, llm, Repository(db))
        finally:
            await llm.close()
            await db.close()
    print(summary_text(profile))
    if args.dry_run:
        print("(dry run: files not written)")
        return 0
    write_profile(profile, settings.profile_file, settings.profile_md_file)
    print(f"Written: {settings.profile_file}\n         {settings.profile_md_file}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--llm", action="store_true", help="polish summary/services via OpenRouter (compact data only)")
    ap.add_argument("--dry-run", action="store_true", help="print the summary, write nothing")
    sys.exit(asyncio.run(run(ap.parse_args())))


if __name__ == "__main__":
    main()
