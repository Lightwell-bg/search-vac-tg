"""Regression tests for the Codex review of the journal/retention feature (7 findings)."""
import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import delete, func, select

import src.db.repository as repo_mod
import src.filtering.pipeline as pl
from src import journal as jr
from src.bot.menu import (
    MenuHandlers, MenuStates, TIMEZONE_PRESETS, interval_text, parse_menu_callback, settings_keyboard,
    timezone_keyboard,
)
from src.bot.stats_format import format_stats
from src.config import _valid_retention
from src.db.database import Database
from src.db.models import Job, JobSource, JobStatus, Message
from src.settings_store import RuntimeSettings, retention_max, retention_min

from .helpers import TECH_TEXT, build_pipeline, make_post
from .test_bot_journal import make_cb, make_msg, make_repo, make_state
from .test_journal import NOW, add, env


# ------------------------------------------------------------------ 1. cleanup vs orphan recovery

async def test_create_job_returns_none_when_message_missing(repo):
    assert await repo.create_job(999, "h", "t", None, None) is None
    async with repo.db.session() as s:
        assert (await s.execute(select(func.count()).select_from(Job))).scalar_one() == 0


async def test_orphan_deleted_between_selection_and_ingest_creates_no_job(repo, monkeypatch):
    pipe, jev, llm, actions, notifier = build_pipeline(repo)
    original_create = repo.create_job
    calls = {"n": 0}

    async def fail_once(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return await original_create(*a, **k)

    monkeypatch.setattr(repo, "create_job", fail_once)
    assert await pipe.process_post(make_post(TECH_TEXT)) == "failed"      # leaves an orphan message
    monkeypatch.setattr(pl, "ORPHAN_GRACE", timedelta(0))

    original_select = repo.orphan_messages

    async def select_then_cleanup(*a, **k):
        rows = await original_select(*a, **k)
        async with repo.db.session() as s:           # cleanup removes it right after the selection
            await s.execute(delete(Message))
            await s.commit()
        return rows

    monkeypatch.setattr(repo, "orphan_messages", select_then_cleanup)
    await pipe.retry_pending()
    async with repo.db.session() as s:
        assert (await s.execute(select(func.count()).select_from(Job))).scalar_one() == 0
        assert (await s.execute(select(func.count()).select_from(JobSource))).scalar_one() == 0
    assert notifier.calls == []


# ------------------------------------------------------------------ 2. every status is classified

def test_every_job_status_is_explicitly_classified():
    groups = {"protected": set(JobStatus.PROTECTED_FROM_CLEANUP), "retryable": set(JobStatus.RETRYABLE),
              "deletable": set(JobStatus.DELETABLE)}
    statuses = set(JobStatus.all())
    assert len(statuses) >= 13
    for status in statuses:
        assert any(status in g for g in groups.values()), f"{status} is not classified for cleanup"
    assert groups["deletable"] <= statuses
    assert not groups["deletable"] & (groups["protected"] | groups["retryable"])
    assert {JobStatus.NOTIFIED, JobStatus.NOTIFY_UNCERTAIN, JobStatus.NOTIFYING} <= groups["protected"]


@pytest.mark.parametrize("status", sorted(set(JobStatus.all()) - set(JobStatus.DELETABLE)))
async def test_cleanup_never_deletes_protected_or_retryable(repo, status):
    msg, job = await add(repo, status=status, age_days=90)
    await jr.cleanup_old(repo, 30)
    assert await repo.get_job(job) is not None
    assert await repo.message_row_exists(msg)


@pytest.mark.parametrize("status", sorted(JobStatus.DELETABLE))
async def test_cleanup_deletes_deletable_statuses(repo, status):
    msg, job = await add(repo, status=status, age_days=90)
    await jr.cleanup_old(repo, 30)
    assert await repo.get_job(job) is None and not await repo.message_row_exists(msg)


# ------------------------------------------------------------------ 3. secondary sources of kept jobs

async def test_protected_job_keeps_all_sources_and_messages(repo):
    m1, job = await add(repo, status=JobStatus.NOTIFY_UNCERTAIN, age_days=90)
    m2, _ = await add(repo, age_days=85, dup_of=job)
    m3, _ = await add(repo, age_days=80, dup_of=job)
    await jr.cleanup_old(repo, 30)
    assert all([await repo.message_row_exists(m) for m in (m1, m2, m3)])
    assert len(await repo.get_job_sources(job)) == 3


# ------------------------------------------------------------------ 4. all-time stats survive cleanup

async def test_stats_identical_before_and_after_cleanup(repo):
    _, j1 = await add(repo, status=JobStatus.RULE_REJECTED, age_days=90, contact_status="paid_contact")
    await add(repo, age_days=89, dup_of=j1)
    _, j2 = await add(repo, status=JobStatus.JEV_REJECTED, age_days=80)
    _, j3 = await add(repo, status=JobStatus.NOTIFIED, age_days=70)
    await repo.save_notification(j3, 1, 5)
    await add(repo, status=JobStatus.RULE_REJECTED, age_days=1)
    await add(repo, text="", job=False, age_days=75)
    for j in (j1, j2):
        await repo.log_jev_usage(job_id=j, purpose="job_classification", decision="reject", cost_usd=0.25)
    await repo.log_jev_usage(job_id=j2, purpose="job_classification", decision="review", error="e",
                             fallback_used=True, cost_usd=0.5)
    await repo.log_llm_usage(job_id=j2, purpose="job_review", model="m", cost_usd=0.125)
    await repo.log_llm_usage(job_id=j1, purpose="application_generation", model="m", cost_usd=0.0625)
    before = await repo.get_stats()
    res = await jr.cleanup_old(repo, 30)
    assert res["jobs"] == 2 and res["messages"] >= 4
    after = await repo.get_stats()
    live_since = after.pop("since")
    before.pop("since")
    assert after == before
    assert before["messages_received"] == 6 and before["duplicates"] == 1 and before["jev_processed"] == 3
    assert live_since is not None
    # a second cleanup must not double count
    await jr.cleanup_old(repo, 30)
    again = await repo.get_stats()
    again.pop("since")
    assert again == before


async def test_cleanup_archive_is_one_transaction_with_delete(repo, monkeypatch):
    await add(repo, status=JobStatus.RULE_REJECTED, age_days=90)

    async def boom(*a, **k):
        raise RuntimeError("fail before commit")

    original = repo_mod.Repository._archive_stats
    monkeypatch.setattr(repo_mod.Repository, "_archive_stats", staticmethod(boom))
    with pytest.raises(RuntimeError):
        await jr.cleanup_old(repo, 30)
    monkeypatch.setattr(repo_mod.Repository, "_archive_stats", original)
    assert (await repo.get_stats())["messages_received"] == 1
    assert "stats_archive" not in await repo.get_settings()


def test_format_stats_shows_all_time_and_since():
    from datetime import datetime, timezone
    text = format_stats({"since": datetime(2026, 9, 30, 22, 30, tzinfo=timezone.utc)}, "Europe/Sofia")
    assert "Статистика за всё время" in text
    assert "Подробный журнал хранится с 01.10.2026" in text      # 22:30 UTC = 01:30 next day in Sofia
    assert "хранится с" not in format_stats({})


# ------------------------------------------------------------------ 5. retention vs dedup window

def test_retention_bounds_follow_dedup_window():
    assert (retention_min(14), retention_max(14)) == (14, 365)
    assert (retention_min(400), retention_max(400)) == (400, 400)
    assert (retention_min(3), retention_max(3)) == (7, 365)


def test_config_retention_never_below_min(caplog):
    with caplog.at_level(logging.WARNING, logger="config"):
        assert _valid_retention(30, 400) == 400
        assert _valid_retention(500, 400) == 400
        assert _valid_retention(900, 14) == 365
        assert _valid_retention(5, 3) == 7
    assert "out of range" in caplog.text
    assert _valid_retention(30, 14) == 30


async def test_runtime_retention_with_window_400(repo):
    rs = await RuntimeSettings.load(repo, env(dedup_window_days=400, log_retention_days=30))
    assert rs.log_retention_days == 400           # config below MIN is raised, never kept
    with pytest.raises(ValueError, match="400"):
        await rs.set("log_retention_days", 399)
    await rs.set("log_retention_days", 400)
    with pytest.raises(ValueError):
        await rs.set("log_retention_days", 401)


async def test_startup_warns_when_config_retention_out_of_range(repo, caplog):
    with caplog.at_level(logging.WARNING, logger="settings"):
        rs = await RuntimeSettings.load(repo, env(dedup_window_days=60, log_retention_days=10))
    assert rs.log_retention_days == 60 and "out of range" in caplog.text
    assert (await RuntimeSettings.load(repo, env(log_retention_days=9999))).log_retention_days == 365


# ------------------------------------------------------------------ 6. journal performance

async def test_composite_index_created_on_old_db(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'o.db').as_posix()}")
    await db.init()
    async with db.engine.begin() as conn:
        await conn.exec_driver_sql("DROP INDEX ix_messages_received_id")
    await db.init()
    async with db.engine.begin() as conn:
        cols = (await conn.exec_driver_sql("PRAGMA index_info(ix_messages_received_id)")).all()
    assert [c[2] for c in cols] == ["received_at", "id"]
    await db.close()


async def test_journal_order_is_received_at_then_id_desc(repo):
    a, _ = await add(repo, status=JobStatus.RULE_REJECTED, age_days=0)
    b, _ = await add(repo, status=JobStatus.RULE_REJECTED, age_days=0)
    async with repo.db.session() as s:   # identical timestamps: id breaks the tie
        ts = (await s.execute(select(Message.received_at).where(Message.id == a))).scalar_one()
        await s.execute(Message.__table__.update().values(received_at=ts))
        await s.commit()
    ids = [e.message_row_id for e in await repo.journal(None, "all", 10, 0)]
    assert ids == [b, a]


async def test_journal_counts_cached_and_invalidated_by_cleanup(repo, monkeypatch):
    await add(repo, status=JobStatus.RULE_REJECTED, age_days=90)
    await add(repo, status=JobStatus.RULE_REJECTED, age_days=0.1)
    since = NOW - timedelta(days=200)
    first = await repo.journal_counts(since)
    await add(repo, status=JobStatus.RULE_REJECTED, age_days=0.05)
    assert await repo.journal_counts(since) == first                     # served from the cache
    first["all"] = 999                                                   # callers get a copy
    assert (await repo.journal_counts(since))["all"] == 2
    await jr.cleanup_old(repo, 30)                                       # invalidates
    assert (await repo.journal_counts(since))["all"] == 2                # 3 stored - 1 deleted


async def test_journal_counts_cache_expires_after_ttl(repo, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(repo_mod, "_monotonic", lambda: clock["t"])
    await add(repo, status=JobStatus.RULE_REJECTED)
    assert (await repo.journal_counts(None))["all"] == 1
    await add(repo, status=JobStatus.RULE_REJECTED)
    assert (await repo.journal_counts(None))["all"] == 1
    clock["t"] += repo_mod.COUNTS_CACHE_TTL_SEC + 1
    assert (await repo.journal_counts(None))["all"] == 2


# ------------------------------------------------------------------ 7. timezone as a live setting

async def test_timezone_setting_validated_persisted_and_live(repo):
    rs = await RuntimeSettings.load(repo, env(timezone="Europe/Sofia"))
    assert rs.timezone == "Europe/Sofia" and "timezone" in rs.as_dict()
    seen = []
    rs.subscribe(lambda r: seen.append(r.timezone))
    await rs.set("timezone", " Europe/Warsaw ")
    assert rs.timezone == "Europe/Warsaw" and seen == ["Europe/Warsaw"]
    assert (await RuntimeSettings.load(repo, env(timezone="UTC"))).timezone == "Europe/Warsaw"
    for bad in ("Not/AZone", "", "   ", 5, None, "../etc/passwd", "x" * 100):
        with pytest.raises(ValueError, match="часовой пояс"):
            await rs.set("timezone", bad)
    assert rs.timezone == "Europe/Warsaw"


async def test_invalid_stored_or_config_timezone_falls_back(repo):
    await repo.set_setting("timezone", "Bad/Zone")
    assert (await RuntimeSettings.load(repo, env(timezone="Europe/Berlin"))).timezone == "Europe/Berlin"
    assert (await RuntimeSettings.load(MagicMock(get_settings=AsyncMock(return_value={})),
                                       env(timezone="Bad/Zone"))).timezone == "UTC"


def _rs():
    repo = MagicMock()
    repo.set_setting = AsyncMock()
    return RuntimeSettings(repo, notify_score=60, high_fit_score=80, show_paid_contact=False,
                           notifications_paused=False, openrouter_model="a/b", timezone="Europe/Sofia")


def test_main_menu_has_timezone_button_and_presets():
    rs = _rs()
    assert "🕒 Часовой пояс: Europe/Sofia" in [b.text for r in settings_keyboard(rs).inline_keyboard for b in r]
    assert parse_menu_callback("tz:0") == ("tz", "0")
    kb = timezone_keyboard(rs)
    labels = [b.text for r in kb.inline_keyboard for b in r]
    for name in ("Europe/Sofia", "Europe/Moscow", "Europe/Kyiv", "Europe/Berlin", "UTC", "Asia/Almaty"):
        assert name in TIMEZONE_PRESETS and any(name in x for x in labels)
    assert "✅ Europe/Sofia" in labels and any("Своё" in x for x in labels)


async def test_timezone_preset_applies_live_to_interval_screen():
    from datetime import datetime, timezone
    rs = _rs()
    h = MenuHandlers(make_repo(), rs)
    cb = make_cb("m:tz")
    await h.on_callback(cb, make_state())
    assert "Europe/Sofia" in cb.message.edit_text.call_args.args[0]
    cb = make_cb("tz:1")
    await h.on_callback(cb, make_state())
    assert rs.timezone == "Europe/Moscow"
    listener = SimpleNamespace(last_poll_at=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc), last_poll_new=2)
    assert "13:00" in interval_text(rs, listener)          # Moscow = UTC+3
    await h.on_callback(make_cb("tz:99"), make_state())
    await h.on_callback(make_cb("tz:abc"), make_state())
    assert rs.timezone == "Europe/Moscow"


async def test_timezone_custom_flow():
    rs = _rs()
    h = MenuHandlers(make_repo(), rs)
    st = make_state()
    await h.on_callback(make_cb("tz:custom"), st)
    st.set_state.assert_awaited_with(MenuStates.waiting_timezone)
    msg = make_msg(None)                                    # non-text -> reminder, state kept
    await h.on_timezone_input(msg, st)
    assert "IANA" in msg.answer.call_args.args[0]
    st2 = make_state()
    msg = make_msg("Mars/Base")
    await h.on_timezone_input(msg, st2)
    assert "Неизвестный часовой пояс" in msg.answer.call_args.args[0]
    st2.clear.assert_not_awaited()
    msg = make_msg("Europe/Warsaw")
    await h.on_timezone_input(msg, st2)
    assert rs.timezone == "Europe/Warsaw"
    st2.clear.assert_awaited()


def test_timezone_state_registered_after_commands():
    router = MagicMock()
    MenuHandlers(make_repo(), _rs()).register(router)
    names = [c.args[0].__name__ for c in router.message.register.call_args_list]
    assert "on_timezone_input" in names and names.index("cmd_cancel") < names.index("on_timezone_input")


async def test_stats_command_uses_live_timezone():
    from datetime import datetime, timezone
    rs = _rs()
    rs.timezone = "Asia/Almaty"            # UTC+5
    repo = make_repo()
    repo.get_stats = AsyncMock(return_value={"since": datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc)})
    h = MenuHandlers(repo, rs)
    msg = make_msg("/stats")
    await h.cmd_stats(msg, make_state())
    assert "хранится с 01.10.2026" in msg.answer.call_args.args[0]
