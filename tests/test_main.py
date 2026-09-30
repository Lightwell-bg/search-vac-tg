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
    monkeypatch.setattr(main_mod, "load_profile", lambda _f: {})
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
