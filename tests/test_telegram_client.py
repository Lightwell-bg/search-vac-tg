import os
import stat
import sys
from types import SimpleNamespace

import pytest
from telethon import errors

from src.telegram.client import TelegramService, secure_session_files


def service(get_entity):
    svc = object.__new__(TelegramService)
    svc.client = SimpleNamespace(get_entity=get_entity)
    svc.channels = {}
    return svc


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept: list[float] = []

    async def _sleep(s, *a, **k):
        slept.append(s)

    monkeypatch.setattr("asyncio.sleep", _sleep)
    return slept


def flood():
    return errors.FloodWaitError(request=None, capture=5)


async def test_floodwait_retries_same_channel_up_to_three_times(no_sleep):
    calls = []

    async def get_entity(name):
        calls.append(name)
        raise flood()

    refs = await service(get_entity).resolve_channels([SimpleNamespace(username="a"), SimpleNamespace(username="b")])
    assert refs == []
    assert calls == ["a", "a", "a", "b", "b", "b"]
    assert len(no_sleep) == 4  # sleeps between attempts only (2 per channel)


async def test_floodwait_then_not_found_stops_for_that_channel(no_sleep):
    seq = [flood(), ValueError("no such user")]
    calls = []

    async def get_entity(name):
        calls.append(name)
        raise seq[len(calls) - 1]

    assert await service(get_entity).resolve_channels([SimpleNamespace(username="a")]) == []
    assert calls == ["a", "a"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_secure_session_files_sets_modes(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "telegram.session").write_text("x")
    secure_session_files(str(d / "telegram"))
    assert stat.S_IMODE(os.stat(d / "telegram.session").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700


def test_secure_session_files_ignores_missing_file(tmp_path):
    secure_session_files(str(tmp_path / "nothing"))  # must not raise
