"""Alerter: cooldown, enabled switch, error-rate threshold, escaping; pipeline hook; NotifyBot.send_owner."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.alerts import Alerter, clip_escape
from src.jev.schemas import JevError
from src.llm.schemas import LlmError
from src.filtering.pipeline import PipelineOptions

from .helpers import TECH_TEXT, FakeJevClient, FakeLLM, build_pipeline, make_post
from .test_pipeline import JEV_RESULTS


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(**kw):
    sent: list[str] = []

    async def send(text):
        sent.append(text)

    clock = Clock()
    return Alerter(send, clock=clock, **kw), sent, clock


async def test_cooldown_per_kind():
    a, sent, clock = make(cooldown_sec=3600)
    assert await a.alert("poll_failing", "x") is True
    assert await a.alert("poll_failing", "x") is False        # same kind inside the cooldown
    assert await a.alert("backup_failed", "y") is True         # another kind is independent
    clock.t += 3601
    assert await a.alert("poll_failing", "z") is True
    assert sent == ["x", "y", "z"]


async def test_startup_and_shutdown_bypass_cooldown():
    a, sent, _ = make()
    for _ in range(2):
        assert await a.alert("startup", "up")
        assert await a.alert("shutdown", "down")
    assert sent == ["up", "down", "up", "down"]


async def test_disabled_sends_nothing_even_startup():
    flag = {"on": False}
    a, sent, _ = make(enabled=lambda: flag["on"])
    assert await a.alert("startup", "up") is False
    assert await a.alert("poll_failing", "x") is False
    flag["on"] = True  # live switch, and a disabled alert did not start a cooldown
    assert await a.alert("poll_failing", "x") is True
    assert sent == ["x"]


async def test_send_failure_does_not_raise(caplog):
    async def boom(text):
        raise RuntimeError("telegram down")

    a = Alerter(boom, clock=Clock())
    with caplog.at_level("WARNING"):
        assert await a.alert("startup", "up") is False
    assert "cannot send alert" in caplog.text


async def test_error_rate_threshold_5_in_30_min():
    a, sent, clock = make()
    for _ in range(4):
        assert a.record_error("jev_errors", "boom") is False
        clock.t += 60
    assert a.record_error("jev_errors", "boom") is True
    await asyncio.sleep(0)
    await asyncio.gather(*list(a._tasks))
    assert len(sent) == 1 and "JEV" in sent[0] and "5" in sent[0]
    # cooldown: further errors do not alert again
    a.record_error("jev_errors", "boom")
    await asyncio.gather(*list(a._tasks))
    assert len(sent) == 1


async def test_errors_outside_window_are_forgotten():
    a, sent, clock = make()
    for _ in range(4):
        a.record_error("openrouter_errors", "x")
    clock.t += 31 * 60
    assert a.record_error("openrouter_errors", "x") is False   # old ones expired: count is 1
    assert not a._tasks and sent == []


async def test_error_kinds_are_counted_separately():
    a, _, _ = make()
    for _ in range(3):
        a.record_error("jev_errors", "x")
        a.record_error("openrouter_errors", "x")
    assert a.record_error("jev_errors", "x") is False


async def test_record_error_html_escaped_and_clipped():
    a, sent, _ = make(error_threshold=1)
    a.record_error("jev_errors", "<script>alert(1)</script>" + "a" * 500)
    await asyncio.gather(*list(a._tasks))
    assert "<script>" not in sent[0] and "&lt;script&gt;" in sent[0]
    assert len(sent[0]) < 450


def test_clip_escape():
    assert clip_escape("<b>&") == "&lt;b&gt;&amp;"
    assert clip_escape("x" * 1000).endswith("…") and len(clip_escape("x" * 1000)) == 301
    assert clip_escape(None) == ""


def test_record_error_without_loop_does_not_raise():
    a, sent, _ = make(error_threshold=1)
    assert a.record_error("jev_errors", "x") is True   # no running loop: counted, nothing sent
    assert sent == []


# ------------------------------------------------------------------ pipeline hook

async def test_pipeline_on_error_hook(repo):
    calls = []
    pipe, *_ = build_pipeline(repo, jev=FakeJevClient(JEV_RESULTS["ERROR"]),
                              llm=FakeLLM(LlmError("timeout", "slow")),
                              options=PipelineOptions(jev_fallback_to_openrouter=True))
    pipe.on_error = lambda kind, err: calls.append((kind, type(err)))
    assert await pipe.process_post(make_post(TECH_TEXT)) == "llm_error"
    assert calls == [("jev_errors", JevError), ("openrouter_errors", LlmError)]


async def test_pipeline_on_error_hook_failure_is_swallowed(repo):
    def bad(kind, err):
        raise RuntimeError("hook")

    pipe, *_ = build_pipeline(repo, jev=FakeJevClient(JEV_RESULTS["ERROR"]),
                              options=PipelineOptions(jev_fallback_to_openrouter=False))
    pipe.on_error = bad
    assert await pipe.process_post(make_post(TECH_TEXT)) == "jev_unavailable"


# ------------------------------------------------------------------ NotifyBot.send_owner

async def test_send_owner_plain_html_no_preview():
    from src.bot.bot import NotifyBot

    bot = NotifyBot.__new__(NotifyBot)
    bot.settings = SimpleNamespace(owner_telegram_id=42)
    bot.bot = SimpleNamespace(send_message=AsyncMock())
    await bot.send_owner("✅ ok")
    args, kwargs = bot.bot.send_message.call_args
    assert args == (42, "✅ ok")
    assert kwargs["parse_mode"] == "HTML" and kwargs["link_preview_options"].is_disabled is True


async def test_send_owner_without_owner_raises():
    from src.bot.bot import NotifyBot

    bot = NotifyBot.__new__(NotifyBot)
    bot.settings = SimpleNamespace(owner_telegram_id=None)
    with pytest.raises(RuntimeError):
        await bot.send_owner("x")


# ------------------------------------------------------------------ send_emergency_alert

class _FakeAiogramBot:
    sent: list = []
    fail = False
    closed = 0

    def __init__(self, token, *a, **k):
        self.token = token
        self.session = self

    async def send_message(self, chat_id, text, **k):
        if _FakeAiogramBot.fail:
            raise OSError("telegram down")
        _FakeAiogramBot.sent.append((self.token, chat_id, text))

    async def close(self):
        _FakeAiogramBot.closed += 1


@pytest.fixture
def fake_bot(monkeypatch):
    import aiogram
    _FakeAiogramBot.sent, _FakeAiogramBot.fail, _FakeAiogramBot.closed = [], False, 0
    monkeypatch.setattr(aiogram, "Bot", _FakeAiogramBot)
    return _FakeAiogramBot


async def test_emergency_alert_is_rate_limited_by_marker(tmp_path, fake_bot):
    from types import SimpleNamespace
    from src.alerts import send_emergency_alert
    s = SimpleNamespace(notify_bot_token="tok", owner_telegram_id=42)
    marker = tmp_path / "data" / "integrity_alert.json"
    assert await send_emergency_alert(s, "boom", marker, now=1000.0) is True
    assert fake_bot.sent == [("tok", 42, "boom")] and fake_bot.closed == 1 and marker.exists()
    assert await send_emergency_alert(s, "boom", marker, now=1000.0 + 3600) is False   # inside 6 h
    assert len(fake_bot.sent) == 1
    assert await send_emergency_alert(s, "boom", marker, now=1000.0 + 21600 + 1) is True
    assert len(fake_bot.sent) == 2


async def test_emergency_alert_send_failure_is_swallowed_and_not_marked(tmp_path, fake_bot):
    from types import SimpleNamespace
    from src.alerts import send_emergency_alert
    s = SimpleNamespace(notify_bot_token="tok", owner_telegram_id=42)
    marker = tmp_path / "m.json"
    fake_bot.fail = True
    assert await send_emergency_alert(s, "x", marker, now=1.0) is False
    assert not marker.exists() and fake_bot.closed == 1      # session closed, retried next start
    assert await send_emergency_alert(SimpleNamespace(notify_bot_token="", owner_telegram_id=None),
                                      "x", marker) is False
