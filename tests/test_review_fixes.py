"""Regression tests for the Codex review of the 'manage from the bot' feature."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import text

from src.bot.bot import NotifyBot
from src.config import ChannelConfig
from src.db.database import Database
from src.db.models import JobStatus
from src.db.repository import Repository
from src.filtering.pipeline import PipelineOptions
from src.settings_store import RuntimeSettings
from src.telegram.client import ChannelRef
from src.telegram.listener import ChannelListener

from .helpers import FakeJevClient, FakeNotifier, TECH_TEXT, build_pipeline, jev_answers, make_post
from .test_channel_registry import RegTg, make, ref
from .test_listener import PEER, FakeTg, ScriptedPipeline


# ------------------------------------------------------------------ 1. migration reconcile


async def test_migration_reconciles_old_rows_with_yaml(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'old.db').as_posix()}")
    async with db.engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE channels (id INTEGER PRIMARY KEY, tg_id BIGINT UNIQUE, username VARCHAR(64), "
            "title VARCHAR(256), last_message_id INTEGER, created_at DATETIME)"))
        for i, name in ((1, "chan_a"), (2, "chan_b")):
            await conn.execute(text(
                f"INSERT INTO channels (id, tg_id, username, title, last_message_id, created_at) "
                f"VALUES ({i}, -100{i}, '{name}', 'T', 5, '2026-01-01 00:00:00')"))
    await db.init()
    repo = Repository(db)
    tg = RegTg({"chan_a": ref("chan_a", -1001)})
    lst = make(repo, tg, [ChannelConfig("chan_a", click_callbacks=False)])
    await lst.setup()
    rows = {r.username: r for r in await repo.list_channels()}
    assert rows["chan_b"].enabled is False
    assert rows["chan_a"].enabled is True and rows["chan_a"].click_callbacks is False
    assert lst.is_enabled(-1001) and not lst.is_enabled(-1002)
    await repo.set_channel_flags(-1002, enabled=True)  # owner re-enables from the bot
    lst2 = make(repo, tg, [ChannelConfig("chan_a")])
    await lst2.setup()
    assert lst2.is_enabled(-1002)  # one-time only
    await db.close()


# ------------------------------------------------------------------ 2. click / poll enabled checks


async def test_click_allowed_false_for_unknown_disabled_deleted(repo):
    lst = make(repo, RegTg({"chan": ref("chan", -10)}))
    assert lst.click_allowed(-12345) is False
    await lst.add_channel("chan")
    assert lst.click_allowed(-10) is True
    await lst.set_enabled(-10, False)
    assert lst.click_allowed(-10) is False
    await lst.set_enabled(-10, True)
    await lst.remove_channel(-10)
    assert lst.click_allowed(-10) is False


async def test_poll_stops_batch_when_channel_disabled_mid_batch(repo):
    await repo.upsert_channel(PEER, "chan", "Chan")
    holder = {}

    class DisablingPipeline(ScriptedPipeline):
        async def process_post(self, post):
            out = await super().process_post(post)
            if post.message_id == 1:
                await holder["l"].set_enabled(PEER, False)
            return out

    pipe = DisablingPipeline()
    listener = ChannelListener(FakeTg([1, 2, 3]), pipe, repo, [], catchup_limit=50)
    listener.refs = [ChannelRef("chan", PEER, object(), "Chan")]
    holder["l"] = listener
    assert await listener.poll_once() == 1
    assert pipe.calls == [1]
    assert await repo.get_last_message_id(PEER) == 1


# ------------------------------------------------------------------ 3. seeding vs bot mutations


async def test_concurrent_add_channel_and_seeding_one_row(repo):
    tg = RegTg({"chan": ref("chan", -10)})
    lst = make(repo, tg, [ChannelConfig("chan")])
    results = await asyncio.gather(lst.add_channel("chan"), lst.setup(), return_exceptions=True)
    assert not any(isinstance(r, BaseException) for r in results)
    assert len(await repo.list_channels()) == 1


async def test_upsert_channel_is_conflict_safe(repo):
    await asyncio.gather(*[repo.upsert_channel(-10, f"chan{i}x", "T") for i in range(5)])
    rows = await repo.list_channels()
    assert len(rows) == 1 and rows[0].tg_id == -10


async def test_main_runs_setup_before_bot_polling():
    import inspect
    from src import main as main_mod
    src = inspect.getsource(main_mod.run)
    assert src.index("await listener.setup()") < src.index("bot.start_polling()")
    assert "listener.run()" not in src


# ------------------------------------------------------------------ 4. paused rows do not starve retries


async def _make_job(repo, n, status, **fields):
    mid, _ = await repo.save_message(channel_tg_id=100, channel_username="chan_one", message_id=n,
                                     original_text=TECH_TEXT, normalized_text=TECH_TEXT)
    job_id = await repo.create_job(mid, f"hash{n}", TECH_TEXT, "t", None, dedup_key=f"k{n}")
    await repo.update_job(job_id, status=status, **fields)
    return job_id


async def test_paused_accepted_jobs_do_not_starve_llm_error_retry(repo):
    pipe, jev, llm, actions, notifier = build_pipeline(
        repo, jev=FakeJevClient(jev_answers("accept")), options=PipelineOptions(notifications_paused=True))
    for n in range(1, 61):
        await _make_job(repo, n, JobStatus.ACCEPTED, contact_status="direct", contact_value="@x")
    retry_id = await _make_job(repo, 61, JobStatus.LLM_ERROR)
    assert await pipe.retry_pending() == 1
    assert (await repo.get_job(retry_id)).status != JobStatus.LLM_ERROR
    assert notifier.calls == []


# ------------------------------------------------------------------ 5. live high_fit threshold


async def test_card_uses_runtime_high_fit_threshold(repo):
    settings = SimpleNamespace(notify_bot_token="123456:ABCDEF", owner_telegram_id=42, high_fit_score=80,
                               notify_score=60, show_paid_contact=False, openrouter_model="a/b")
    rs = await RuntimeSettings.load(repo, settings)
    await rs.set("high_fit_score", 90)
    bot = NotifyBot(settings, repo, None, {}, runtime_settings=rs)
    sent = []

    async def send_message(chat_id, text, **kw):
        sent.append(text)
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=1)

    bot.bot = SimpleNamespace(send_message=send_message)
    mid, _ = await repo.save_message(channel_tg_id=100, channel_username="c", message_id=1,
                                     original_text="x", normalized_text="x")
    job_id = await repo.create_job(mid, "h", "x", "t", None)
    await repo.update_job(job_id, fit_score=85)
    await bot.notify_job(job_id)
    assert "✅ Возможно подходит — 85/100" in sent[0] and "🔥" not in sent[0]
    await rs.set("high_fit_score", 80)
    await bot.notify_job(job_id)
    assert "🔥" in sent[1]


# ------------------------------------------------------------------ 6. tombstones by peer id


async def test_renamed_deleted_channel_not_recreated(repo):
    cfgs = [ChannelConfig("fresh")]
    lst = make(repo, RegTg({"fresh": ref("fresh", -2)}), cfgs)
    await lst.setup()
    assert await lst.remove_channel(-2) is True
    assert (await repo.get_settings())["deleted_channel_ids"] == [-2]
    # the channel was renamed: the yaml name now resolves to the same peer id under a new username
    lst2 = make(repo, RegTg({"fresh": ref("fresh_new", -2)}), cfgs)
    await lst2.setup()
    assert await repo.list_channels() == [] and lst2.refs == []


async def test_reassigned_username_of_other_channel_not_suppressed(repo):
    cfgs = [ChannelConfig("fresh")]
    lst = make(repo, RegTg({"fresh": ref("fresh", -2)}), cfgs)
    await lst.setup()
    await lst.remove_channel(-2)
    lst2 = make(repo, RegTg({"fresh": ref("fresh", -9)}), cfgs)  # username now belongs to another channel
    await lst2.setup()
    assert [r.tg_id for r in await repo.list_channels()] == [-9]


async def test_add_channel_clears_id_tombstone(repo):
    lst = make(repo, RegTg({"chan": ref("chan", -10)}))
    await repo.set_setting("deleted_channel_ids", [-10, -11])
    assert (await lst.add_channel("chan"))[0] is True
    assert (await repo.get_settings())["deleted_channel_ids"] == [-11]


# ------------------------------------------------------------------ 7. flush_backlog drains everything


async def test_flush_backlog_sends_more_than_one_batch_exactly_once(repo):
    n_jobs = 130
    pipe, jev, llm, actions, notifier = build_pipeline(repo, notifier=FakeNotifier())
    ids = [await _make_job(repo, n, JobStatus.ACCEPTED, contact_status="direct", contact_value="@x")
           for n in range(1, n_jobs + 1)]
    assert await pipe.flush_backlog() == n_jobs
    assert sorted(notifier.calls) == ids and len(notifier.calls) == n_jobs
    assert await pipe.flush_backlog() == 0


async def test_flush_backlog_stops_without_progress(repo):
    pipe, jev, llm, actions, notifier = build_pipeline(repo, notifier=FakeNotifier(error=RuntimeError("down")))
    await _make_job(repo, 1, JobStatus.ACCEPTED, contact_status="direct", contact_value="@x")
    await pipe.flush_backlog()  # must terminate
    assert len(notifier.calls) <= 10
