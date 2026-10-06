"""Entry point: python -m src.main

Runs the Telethon channel monitor and the aiogram notification bot in one event loop.
"""
from __future__ import annotations

import asyncio
import html
import logging
import signal
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy.exc

from . import __version__
from .alerts import (KIND_DB_INTEGRITY, KIND_SHUTDOWN, KIND_STARTUP, Alerter, clip_escape,
                     send_emergency_alert)
from .backup import BackupService, sqlite_path
from .bot.bot import NotifyBot
from .config import load_channels, load_settings, setup_logging
from .db.database import Database
from .db.repository import Repository
from .filtering.deduplicator import Deduplicator
from .filtering.pipeline import Pipeline, PipelineOptions
from .filtering.rules import RuleFilter
from .health import heartbeat_loop
from .jev.classifier import JevClassifier
from .jev.client import JevClient
from .llm.openrouter import OpenRouterClient
from .profile.service import ProfileService
from .settings_store import RuntimeSettings
from .telegram.client import TelegramService
from .telegram.contact_resolver import ContactResolver
from .telegram.listener import ChannelListener

log = logging.getLogger("main")

SHUTDOWN_ALERT_TIMEOUT_SEC = 5
WORKER_STOP_TIMEOUT_SEC = 10
INTEGRITY_MARKER_NAME = "integrity_alert.json"  # next to the heartbeat file (data/)
DB_ERRORS = (sqlite3.DatabaseError, sqlalchemy.exc.DatabaseError)


def build_pipeline(s, repo, *, telegram_actions=None, notifier=None, llm=None, jev_client=None,
                   channels=None, click_policy=None, profile_service=None) -> Pipeline:
    """Wire the pipeline from settings. Also used by scripts/dry_run.py."""
    rules = RuleFilter.from_file(s.filter_file, s.min_text_length)
    profile = (profile_service or ProfileService(s)).load()
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
            click_policy=click_policy or (lambda post: (post.channel_username or "").lower() not in disabled)),
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


async def _flush(pipeline) -> None:
    try:
        n = await pipeline.flush_backlog()
        log.info("backlog flushed after unpause: %s jobs processed", n)
    except Exception:
        log.exception("ERROR flushing backlog after unpause")


async def run() -> int:
    s = load_settings()
    setup_logging(s)
    missing = s.missing_for_run()
    if missing:
        log.error("fill these variables in .env: %s", ", ".join(missing))
        return 2
    channels = load_channels(s.channels_file)  # seed only: the DB is the source of truth afterwards

    db = Database(s.database_url)
    integrity = "ok"
    try:
        await db.init()
        integrity = await db.quick_check()
    except DB_ERRORS as e:  # the file is not a usable database: the normal bot cannot start
        log.error("ERROR database is corrupted, not starting: %s: %s", type(e).__name__, e)
        await send_emergency_alert(
            s, "🛑 База данных повреждена, бот не запущен. Восстановите из бэкапа: "
               f"docs/DEPLOY.md, раздел «Бэкапы».\n{clip_escape(f'{type(e).__name__}: {e}')}",
            Path(s.heartbeat_file).parent / INTEGRITY_MARKER_NAME)
        try:
            await db.close()
        except Exception:  # noqa: BLE001
            pass
        return 2
    except Exception as e:  # noqa: BLE001 - a failing check must not stop the bot
        integrity = f"quick_check failed: {type(e).__name__}: {e}"
    if integrity != "ok":
        log.error("ERROR database integrity check failed: %s", integrity)
    repo = Repository(db)
    tg = TelegramService(s.session_file, s.telegram_api_id, s.telegram_api_hash,
                         s.flood_sleep_threshold, s.contact_click_delay_sec)
    llm = OpenRouterClient.from_settings(s)
    runtime = await RuntimeSettings.load(repo, s)
    profile_service = ProfileService(s)
    await profile_service.startup()  # rebuild the uploads cache if it is missing/stale, before the pipeline
    holder: dict = {}  # listener is created after the pipeline; click policy reads it lazily
    pipeline = build_pipeline(
        s, repo, telegram_actions=tg, notifier=None, llm=llm, channels=channels,
        profile_service=profile_service,
        click_policy=lambda post: holder["listener"].click_allowed(post.channel_tg_id)
        if "listener" in holder else True)
    listener = ChannelListener(tg, pipeline, repo, channels, catchup_limit=s.catchup_limit,
                               poll_interval_sec=s.poll_interval_sec, retry_interval_sec=s.retry_interval_sec)
    holder["listener"] = listener
    bot = NotifyBot(s, repo, llm, profile_service.load(), runtime_settings=runtime, listener=listener,
                    pipeline=pipeline, profile_service=profile_service)
    profile_service.subscribe(pipeline.set_profile)
    profile_service.subscribe(bot.set_profile)
    pipeline.notifier = bot
    alerter = Alerter(bot.send_owner, enabled=lambda: runtime.alerts_enabled)
    bot.alerter = alerter
    pipeline.on_error = lambda kind, err: alerter.record_error(kind, err)
    db_file = sqlite_path(s.database_url)
    backup_service = None
    if db_file is not None:
        backup_service = BackupService(db_file, s.session_file, s.backup_dir, lambda: runtime.backup_keep,
                                       alerter=alerter)
    bot.backup_service = backup_service

    state = {"paused": runtime.notifications_paused}
    flush_tasks: set[asyncio.Task] = set()

    def on_settings_change(rs: RuntimeSettings) -> None:
        rs.apply_to(pipeline.opt, llm)
        if listener.poll_interval != rs.poll_interval_sec:
            listener.set_poll_interval(rs.poll_interval_sec)
        if listener.retention_days != rs.log_retention_days:
            listener.retention_days = rs.log_retention_days
            t = asyncio.get_running_loop().create_task(listener.run_cleanup())  # apply the new period now
            flush_tasks.add(t)
            t.add_done_callback(flush_tasks.discard)
        was_paused, state["paused"] = state["paused"], rs.notifications_paused
        if was_paused and not rs.notifications_paused:
            # unpaused: send the held (ACCEPTED) backlog right away, no second contact click
            t = asyncio.get_running_loop().create_task(_flush(pipeline))
            flush_tasks.add(t)
            t.add_done_callback(flush_tasks.discard)

    runtime.apply_to(pipeline.opt, llm)
    listener.poll_interval = runtime.poll_interval_sec
    listener.retention_days = runtime.log_retention_days
    runtime.subscribe(on_settings_change)
    rc = 0
    started = False
    stop_signal: list[str] = []
    bg_tasks: list[asyncio.Task] = []
    workers: list[asyncio.Task] = []  # listener + bot polling
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()

    def _on_signal(name: str) -> None:
        if stop_signal:
            return  # already stopping
        stop_signal.append(name)
        log.info("received %s, shutting down", name)
        if main_task is not None:
            main_task.cancel()  # cancels the running wait(listener, bot) and runs the finally below

    handled: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, sig.name)
            handled.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            pass  # Windows / not the main thread: no signal handlers

    try:
        started_at = datetime.now(timezone.utc)
        bg_tasks.append(asyncio.create_task(
            heartbeat_loop(s.heartbeat_file, listener, started_at, alerter=alerter), name="heartbeat"))
        if backup_service is not None:
            bg_tasks.append(asyncio.create_task(
                backup_service.run_loop(lambda: s.backup_hour, lambda: runtime.timezone), name="backup"))
        await tg.start()
        log.info("JEV: %s model=%s; OpenRouter model=%s; notify>=%s",
                 s.jev_url, s.jev_model, runtime.openrouter_model, runtime.notify_score)
        # Telethon handles updates/reconnects in its own background tasks once connected
        # registry seeding must finish before the bot can mutate channels
        await listener.setup()
        started = True
        bg_tasks.append(asyncio.create_task(_startup_alerts(alerter, integrity), name="startup-alerts"))
        workers.append(asyncio.create_task(listener.run_loop(), name="listener"))
        workers.append(asyncio.create_task(bot.start_polling(), name="bot-polling"))
        # not gather(): if one side dies the other must be stopped, not left running
        done, _ = await asyncio.wait(set(workers[-2:]), return_when=asyncio.FIRST_COMPLETED)
        for t in workers[-2:]:
            if t not in done:
                t.cancel()
        await asyncio.gather(*workers[-2:], return_exceptions=True)
        for t in done:
            if not t.cancelled() and t.exception() is not None:
                raise t.exception()
    except asyncio.CancelledError:
        if not stop_signal:
            raise
        main_task.uncancel()  # a signal-driven stop is a clean exit, not a crash
    except RuntimeError as e:
        log.error("%s", e)  # startup errors carry their own hint (e.g. the login command)
        rc = 2
    finally:
        for sig in handled:
            loop.remove_signal_handler(sig)
        # every spawned task must be stopped before the clients/DB it uses are closed
        pending = [*workers, *bg_tasks, *flush_tasks]
        for task in pending:
            task.cancel()
        if pending:
            try:
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True),
                                       WORKER_STOP_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                log.warning("some background tasks did not stop within %s s", WORKER_STOP_TIMEOUT_SEC)
        if started:
            try:  # before bot.close(): the alert needs the bot session
                await asyncio.wait_for(alerter.alert(KIND_SHUTDOWN, "⏹ Бот остановлен"),
                                       SHUTDOWN_ALERT_TIMEOUT_SEC)
            except Exception:  # noqa: BLE001 - TimeoutError included
                log.warning("shutdown alert not sent")
        await bot.close()
        await llm.close()
        await pipeline.jev.client.close()
        await tg.stop()
        await db.close()
    return rc


async def _startup_alerts(alerter: Alerter, integrity: str) -> None:
    """Owner notices after start: "started" and, if the DB check failed, "db_integrity"."""
    await alerter.alert(KIND_STARTUP, f"✅ Бот запущен (версия {html.escape(__version__)})")
    if integrity != "ok":
        await alerter.alert(KIND_DB_INTEGRITY,
                            f"🛑 Проверка целостности БД не пройдена: {clip_escape(integrity)}. "
                            "Остановите сервис и восстановите бэкап (scripts/restore_backup.py)")


def main() -> None:
    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
