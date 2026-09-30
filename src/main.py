"""Entry point: python -m src.main

Runs the Telethon channel monitor and the aiogram notification bot in one event loop.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from .bot.bot import NotifyBot
from .config import load_channels, load_settings, setup_logging
from .db.database import Database
from .db.repository import Repository
from .filtering.deduplicator import Deduplicator
from .filtering.pipeline import Pipeline, PipelineOptions
from .filtering.rules import RuleFilter
from .jev.classifier import JevClassifier
from .jev.client import JevClient
from .llm.openrouter import OpenRouterClient
from .profile.loader import load_profile
from .telegram.client import TelegramService
from .telegram.contact_resolver import ContactResolver
from .telegram.listener import ChannelListener

log = logging.getLogger("main")


def build_pipeline(s, repo, *, telegram_actions=None, notifier=None, llm=None, jev_client=None,
                   channels=None) -> Pipeline:
    """Wire the pipeline from settings. Also used by scripts/dry_run.py."""
    rules = RuleFilter.from_file(s.filter_file, s.min_text_length)
    profile = load_profile(s.profile_file)
    if profile.get("provisional"):
        log.warning("using PROVISIONAL profile: add materials and run scripts/rebuild_profile.py")
    jev = JevClassifier(jev_client or JevClient.from_settings(s), s.jev_min_confidence, s.jev_max_text_chars)
    llm = llm or (OpenRouterClient.from_settings(s) if s.openrouter_api_key and s.openrouter_model else None)
    disabled = {c.username.lower() for c in (channels or []) if not c.click_callbacks}
    return Pipeline(
        repo=repo,
        rules=rules,
        dedup=Deduplicator(repo, s.dedup_fuzzy_threshold, s.dedup_window_days, s.dedup_max_candidates),
        jev=jev,
        llm=llm,
        resolver=ContactResolver(
            telegram_actions, rules.ignore_contacts,
            click_policy=lambda post: (post.channel_username or "").lower() not in disabled),
        notifier=notifier,
        profile=profile,
        options=PipelineOptions(
            notify_score=s.notify_score,
            high_fit_score=s.high_fit_score,
            jev_fallback_to_openrouter=s.jev_fallback_to_openrouter,
            show_paid_contact=s.show_paid_contact,
            retry_limit=s.retry_limit,
        ),
    )


async def run() -> int:
    s = load_settings()
    setup_logging(s)
    missing = s.missing_for_run()
    if missing:
        log.error("fill these variables in .env: %s", ", ".join(missing))
        return 2
    channels = load_channels(s.channels_file)
    if not channels:
        log.error("no enabled channels in %s", s.channels_file)
        return 2

    db = Database(s.database_url)
    await db.init()
    repo = Repository(db)
    tg = TelegramService(s.session_file, s.telegram_api_id, s.telegram_api_hash,
                         s.flood_sleep_threshold, s.contact_click_delay_sec)
    llm = OpenRouterClient.from_settings(s)
    bot = NotifyBot(s, repo, llm, load_profile(s.profile_file))
    pipeline = build_pipeline(s, repo, telegram_actions=tg, notifier=bot, llm=llm, channels=channels)
    listener = ChannelListener(tg, pipeline, repo, channels, catchup_limit=s.catchup_limit,
                               poll_interval_sec=s.poll_interval_sec, retry_interval_sec=s.retry_interval_sec)
    try:
        await tg.start()
        log.info("JEV: %s model=%s; OpenRouter model=%s; notify>=%s",
                 s.jev_url, s.jev_model, s.openrouter_model, s.notify_score)
        # Telethon handles updates/reconnects in its own background tasks once connected
        await asyncio.gather(listener.run(), bot.start_polling())
    finally:
        await bot.close()
        await llm.close()
        await pipeline.jev.client.close()
        await tg.stop()
        await db.close()
    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
