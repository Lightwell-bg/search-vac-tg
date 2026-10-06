"""RuntimeSettings (DB-backed, live-applied), DB migration and notification pause."""
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from src.db.database import Database
from src.db.models import JobStatus
from src.db.repository import Repository
from src.filtering.pipeline import PipelineOptions
from src.settings_store import RuntimeSettings
from src.telegram.contact_resolver import CallbackAnswer
from src.telegram.parser import ButtonInfo

from .helpers import FakeActions, FakeJevClient, FakeLLM, build_pipeline, make_post
from .test_pipeline import JEV_RESULTS, NO_CONTACT_TEXT


def env(**kw):
    base = dict(notify_score=65, high_fit_score=80, show_paid_contact=False, openrouter_model="a/b", poll_interval_sec=120)
    base.update(kw)
    return SimpleNamespace(**base)


async def test_defaults_from_env(repo):
    rs = await RuntimeSettings.load(repo, env(notify_score=70, show_paid_contact=True))
    assert rs.as_dict() == {"notify_score": 70, "high_fit_score": 80, "show_paid_contact": True,
                            "notifications_paused": False, "openrouter_model": "a/b",
                            "poll_interval_sec": 120, "log_retention_days": 30,
                            "timezone": "Europe/Sofia", "alerts_enabled": True, "backup_keep": 7}


async def test_db_overrides_env(repo):
    rs = await RuntimeSettings.load(repo, env())
    await rs.set("notify_score", 50)
    await rs.set("openrouter_model", "openai/gpt-4o-mini")
    await rs.set("notifications_paused", True)
    again = await RuntimeSettings.load(repo, env(notify_score=99, high_fit_score=99))
    assert again.notify_score == 50 and again.openrouter_model == "openai/gpt-4o-mini"
    assert again.notifications_paused is True and again.high_fit_score == 99


async def test_invalid_stored_value_ignored(repo):
    await repo.set_setting("notify_score", "abc")
    rs = await RuntimeSettings.load(repo, env())
    assert rs.notify_score == 65


@pytest.mark.parametrize("key,value", [
    ("notify_score", -1), ("notify_score", 101), ("notify_score", True), ("notify_score", "x"),
    ("notify_score", 81),  # above high_fit_score=80
    ("high_fit_score", 60),  # below notify_score=65
    ("show_paid_contact", "yes"), ("notifications_paused", 1),
    ("poll_interval_sec", 45), ("poll_interval_sec", 86401), ("poll_interval_sec", True), ("poll_interval_sec", "x"),
    ("poll_interval_sec", 0),
    ("openrouter_model", ""), ("openrouter_model", "   "), ("openrouter_model", "nomodel"),
    ("openrouter_model", "a/b c"), ("openrouter_model", "a/" + "b" * 100), ("unknown", 1),
])
async def test_validation_errors(repo, key, value):
    rs = await RuntimeSettings.load(repo, env())
    with pytest.raises(ValueError) as e:
        await rs.set(key, value)
    assert any("а" <= ch.lower() <= "я" for ch in str(e.value))  # Russian message
    assert (await repo.get_settings()) == {}  # nothing persisted


async def test_valid_values(repo):
    rs = await RuntimeSettings.load(repo, env())
    await rs.set("notify_score", "70")
    await rs.set("high_fit_score", 100)
    await rs.set("openrouter_model", "anthropic/claude-3.5-sonnet:beta")
    await rs.set("openrouter_model", "~vendor/model-latest")
    assert (rs.notify_score, rs.high_fit_score) == (70, 100)


async def test_poll_interval_persisted_and_overrides_env(repo):
    rs = await RuntimeSettings.load(repo, env())
    await rs.set("poll_interval_sec", 600)
    await rs.set("poll_interval_sec", "60")
    await rs.set("poll_interval_sec", 600)
    again = await RuntimeSettings.load(repo, env(poll_interval_sec=300))
    assert again.poll_interval_sec == 600
    await rs.set("poll_interval_sec", 30000)  # any value in 60..86400 is accepted
    assert (await RuntimeSettings.load(repo, env(poll_interval_sec=300))).poll_interval_sec == 30000
    await repo.set_setting("poll_interval_sec", 45)  # out of range: ignored
    assert (await RuntimeSettings.load(repo, env(poll_interval_sec=300))).poll_interval_sec == 300


async def test_subscribe_applies_to_live_objects(repo):
    rs = await RuntimeSettings.load(repo, env())
    options, llm = PipelineOptions(), FakeLLM()
    seen = []
    rs.subscribe(lambda r: r.apply_to(options, llm))

    async def async_cb(r):
        seen.append(r.notify_score)

    rs.subscribe(async_cb)
    rs.subscribe(lambda r: 1 / 0)  # a broken listener must not break set()
    await rs.set("notify_score", 40)
    await rs.set("openrouter_model", "x/y")
    await rs.set("show_paid_contact", True)
    await rs.set("notifications_paused", True)
    assert options.notify_score == 40 and options.show_paid_contact is True
    assert options.notifications_paused is True and llm.model == "x/y"
    assert seen == [40, 40, 40, 40]


async def test_apply_to_without_llm(repo):
    rs = await RuntimeSettings.load(repo, env())
    opts = PipelineOptions()
    rs.apply_to(opts, None)
    assert opts.high_fit_score == 80


# ------------------------------------------------------------------ migration


async def test_migration_adds_columns_to_old_schema_db(tmp_path):
    path = tmp_path / "old.db"
    db = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with db.engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE channels (id INTEGER PRIMARY KEY, tg_id BIGINT UNIQUE, username VARCHAR(64), "
            "title VARCHAR(256), last_message_id INTEGER, created_at DATETIME)"))
        await conn.execute(text(
            "INSERT INTO channels (id, tg_id, username, title, last_message_id, created_at) "
            "VALUES (1, -1001, 'old_chan', 'Old', 77, '2026-01-01 00:00:00')"))
    await db.init()
    await db.init()  # idempotent
    repo = Repository(db)
    rows = await repo.list_channels()
    assert len(rows) == 1
    ch = rows[0]
    assert (ch.username, ch.last_message_id, ch.enabled, ch.click_callbacks) == ("old_chan", 77, True, True)
    await repo.set_setting("k", {"a": 1})
    assert (await repo.get_settings()) == {"k": {"a": 1}}
    await db.close()


async def test_upsert_keeps_flags_and_delete_keeps_messages(repo):
    await repo.upsert_channel(-1, "one_chan", "One", enabled=True, click_callbacks=False)
    await repo.set_channel_flags(-1, enabled=False)
    await repo.upsert_channel(-1, "one_chan", "New title")
    ch = await repo.get_channel_by_username("@ONE_chan")
    assert ch.title == "New title" and ch.enabled is False and ch.click_callbacks is False
    await repo.save_message(channel_tg_id=-1, channel_username="one_chan", message_id=1, original_text="t")
    assert await repo.delete_channel(-1) is True
    assert await repo.list_channels() == [] and await repo.message_exists(-1, 1)
    assert await repo.delete_channel(-1) is False


# ------------------------------------------------------------------ pause


async def test_pause_keeps_accepted_then_flush_sends_once_without_second_click(repo):
    actions = FakeActions(CallbackAnswer("Контакт: @client_user"))
    pipe, jev, llm, actions, notifier = build_pipeline(
        repo, jev=FakeJevClient(JEV_RESULTS["ACCEPT"]), actions=actions,
        options=PipelineOptions(notifications_paused=True))
    buttons = [ButtonInfo("Получить контакт", "callback", data_hex="00")]
    assert await pipe.process_post(make_post(NO_CONTACT_TEXT, buttons=buttons)) == "paused"
    job = await repo.get_job(1)
    assert job.status == JobStatus.ACCEPTED and job.attempts == 0
    assert job.contact_status == "free" and actions.clicks == 1 and notifier.calls == []

    assert await pipe.retry_pending() == 0  # skipped while paused
    assert notifier.calls == [] and (await repo.get_job(1)).attempts == 0

    pipe.opt.notifications_paused = False
    assert await pipe.flush_backlog() == 1
    job = await repo.get_job(1)
    assert job.status == JobStatus.NOTIFIED and notifier.calls == [1] and actions.clicks == 1
    assert await pipe.flush_backlog() == 0 and notifier.calls == [1]


# ------------------------------------------------------------------ alerts_enabled / backup_keep

async def test_alerts_enabled_and_backup_keep_defaults_from_config(repo):
    rs = await RuntimeSettings.load(repo, env(alerts_enabled=False, backup_keep=12))
    assert rs.alerts_enabled is False and rs.backup_keep == 12


async def test_alerts_enabled_persisted_validated_and_live(repo):
    rs = await RuntimeSettings.load(repo, env())
    seen = []
    rs.subscribe(lambda r: seen.append(r.alerts_enabled))
    await rs.set("alerts_enabled", False)
    assert rs.alerts_enabled is False and seen == [False]
    assert (await RuntimeSettings.load(repo, env())).alerts_enabled is False   # DB wins over config
    for bad in ("yes", 1, None):
        with pytest.raises(ValueError):
            await rs.set("alerts_enabled", bad)
    assert rs.alerts_enabled is False


async def test_backup_keep_persisted_validated_and_live(repo):
    rs = await RuntimeSettings.load(repo, env())
    seen = []
    rs.subscribe(lambda r: seen.append(r.backup_keep))
    await rs.set("backup_keep", "14")
    assert rs.backup_keep == 14 and seen == [14]
    assert (await RuntimeSettings.load(repo, env(backup_keep=3))).backup_keep == 14
    for bad in (0, 61, -1, "abc", True, 2.5, ""):
        with pytest.raises(ValueError, match="от 1 до 60"):
            await rs.set("backup_keep", bad)
    assert rs.backup_keep == 14
    await rs.set("backup_keep", 1)
    await rs.set("backup_keep", 60)


async def test_invalid_stored_backup_keep_ignored_and_bad_config_clamped(repo):
    await repo.set_setting("backup_keep", 999)
    rs = await RuntimeSettings.load(repo, env(backup_keep=5))
    assert rs.backup_keep == 5
    assert (await RuntimeSettings.load(repo, env(backup_keep=0))).backup_keep == 7
