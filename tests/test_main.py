import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src import main as main_mod


async def test_run_returns_2_when_session_unauthorized(monkeypatch, tmp_path):
    s = MagicMock()
    s.missing_for_run.return_value = []
    s.database_url = f"sqlite+aiosqlite:///{(tmp_path / 'm.db').as_posix()}"
    monkeypatch.setattr(main_mod, "load_settings", lambda: s)
    monkeypatch.setattr(main_mod, "setup_logging", lambda _s: None)
    monkeypatch.setattr(main_mod, "load_channels", lambda _f: [SimpleNamespace(username="x")])
    monkeypatch.setattr(main_mod, "ProfileService", MagicMock(return_value=MagicMock(startup=AsyncMock())))
    monkeypatch.setattr(main_mod, "OpenRouterClient", MagicMock(from_settings=lambda _s: MagicMock(close=AsyncMock())))

    jev_client = MagicMock(close=AsyncMock())
    pipeline = MagicMock(jev=MagicMock(client=jev_client))
    monkeypatch.setattr(main_mod, "build_pipeline", lambda *a, **k: pipeline)
    monkeypatch.setattr(main_mod, "ChannelListener", MagicMock())

    bot = MagicMock(close=AsyncMock())
    monkeypatch.setattr(main_mod, "NotifyBot", MagicMock(return_value=bot))

    tg = MagicMock(start=AsyncMock(side_effect=RuntimeError("Telegram session is not authorized")),
                   stop=AsyncMock())
    monkeypatch.setattr(main_mod, "TelegramService", MagicMock(return_value=tg))

    assert await main_mod.run() == 2
    bot.close.assert_awaited()
    tg.stop.assert_awaited()


# ------------------------------------------------------------------ shutdown / failure paths

def _setup_run(monkeypatch, tmp_path, *, listener_run_loop, bot):
    """Wire run() with mocks. The listener's setup() unpauses the runtime, which spawns a flush task.
    Returns the flush state: {"started": Event, "cancelled": bool}."""
    s = MagicMock()
    s.missing_for_run.return_value = []
    s.database_url = f"sqlite+aiosqlite:///{(tmp_path / 'm.db').as_posix()}"
    s.heartbeat_file = tmp_path / "hb.json"
    s.backup_hour = 4
    monkeypatch.setattr(main_mod, "load_settings", lambda: s)
    monkeypatch.setattr(main_mod, "setup_logging", lambda _s: None)
    monkeypatch.setattr(main_mod, "load_channels", lambda _f: [])
    monkeypatch.setattr(main_mod, "ProfileService", MagicMock(return_value=MagicMock(startup=AsyncMock())))
    monkeypatch.setattr(main_mod, "OpenRouterClient", MagicMock(from_settings=lambda _s: MagicMock(close=AsyncMock())))
    callbacks = []

    class FakeRuntime:
        notifications_paused = True
        poll_interval_sec = 300
        log_retention_days = 30
        alerts_enabled = True
        timezone = "UTC"
        backup_keep = 3
        openrouter_model = "m"
        notify_score = 5

        @classmethod
        async def load(cls, repo, s):
            return cls()

        def apply_to(self, *a):
            pass

        def subscribe(self, cb):
            callbacks.append(cb)

    monkeypatch.setattr(main_mod, "RuntimeSettings", FakeRuntime)
    flush = {"started": asyncio.Event(), "cancelled": False}

    async def flush_backlog():
        flush["started"].set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            flush["cancelled"] = True
            raise

    pipeline = MagicMock(jev=MagicMock(client=MagicMock(close=AsyncMock())), flush_backlog=flush_backlog)
    monkeypatch.setattr(main_mod, "build_pipeline", lambda *a, **k: pipeline)

    async def setup():
        callbacks[0](SimpleNamespace(notifications_paused=False, poll_interval_sec=300, log_retention_days=30,
                                     apply_to=lambda *a: None))  # unpause: spawns the flush task

    listener = MagicMock(poll_interval=300, retention_days=30, setup=setup,
                         run_loop=lambda: listener_run_loop(flush))
    monkeypatch.setattr(main_mod, "ChannelListener", MagicMock(return_value=listener))
    monkeypatch.setattr(main_mod, "NotifyBot", MagicMock(return_value=bot))
    monkeypatch.setattr(main_mod, "TelegramService", MagicMock(return_value=MagicMock(start=AsyncMock(), stop=AsyncMock())))
    return flush


async def test_one_branch_failing_cancels_the_other_and_flush_tasks(monkeypatch, tmp_path):
    state = {"bot_cancelled": False}

    async def polling():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            state["bot_cancelled"] = True
            raise

    async def run_loop(flush):
        await flush["started"].wait()
        raise RuntimeError("listener died")

    bot = MagicMock(close=AsyncMock(), start_polling=polling, send_owner=AsyncMock())
    flush = _setup_run(monkeypatch, tmp_path, listener_run_loop=run_loop, bot=bot)
    assert await main_mod.run() == 2
    assert state["bot_cancelled"] and flush["cancelled"]
    bot.close.assert_awaited()


async def test_cancellation_sends_shutdown_alert_before_bot_close_and_returns_0(monkeypatch, tmp_path):
    order = []

    async def run_loop(flush):
        await asyncio.sleep(3600)

    async def polling():
        await asyncio.sleep(3600)

    async def send_owner(text):
        order.append("alert")

    async def close():
        order.append("bot.close")

    bot = MagicMock(close=close, start_polling=polling, send_owner=send_owner)
    flush = _setup_run(monkeypatch, tmp_path, listener_run_loop=run_loop, bot=bot)
    handlers = {}  # emulate SIGTERM: capture the handler the app registers
    monkeypatch.setattr(asyncio.AbstractEventLoop, "add_signal_handler",
                        lambda self, sig, cb, *args: handlers.setdefault("h", (cb, args)), raising=False)
    monkeypatch.setattr(asyncio.AbstractEventLoop, "remove_signal_handler", lambda self, sig: True, raising=False)

    async def trigger():
        await flush["started"].wait()
        await asyncio.sleep(0.05)
        cb, args = handlers["h"]
        cb(*args)

    t = asyncio.create_task(trigger())
    assert await main_mod.run() == 0
    await t
    assert "alert" in order and order.index("alert") < order.index("bot.close")
    assert flush["cancelled"]


async def test_corrupted_db_sends_emergency_alert_and_returns_2(monkeypatch, tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is definitely not a sqlite database" * 100)
    s = MagicMock()
    s.missing_for_run.return_value = []
    s.database_url = f"sqlite+aiosqlite:///{bad.as_posix()}"
    s.heartbeat_file = tmp_path / "hb.json"
    monkeypatch.setattr(main_mod, "load_settings", lambda: s)
    monkeypatch.setattr(main_mod, "setup_logging", lambda _s: None)
    monkeypatch.setattr(main_mod, "load_channels", lambda _f: [])
    emergency = AsyncMock(return_value=True)
    monkeypatch.setattr(main_mod, "send_emergency_alert", emergency)
    tg_cls = MagicMock()
    monkeypatch.setattr(main_mod, "TelegramService", tg_cls)
    assert await main_mod.run() == 2
    emergency.assert_awaited_once()
    args = emergency.await_args.args
    assert args[0] is s and "повреждена" in args[1] and "DEPLOY.md" in args[1]
    assert args[2] == tmp_path / "integrity_alert.json"
    tg_cls.assert_not_called()  # nothing else was started
