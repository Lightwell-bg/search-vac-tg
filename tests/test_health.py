"""Heartbeat file, the healthcheck rules and the poll_failures counter."""
import asyncio
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src import __version__
from src.alerts import Alerter
from src.config import PROJECT_ROOT
from src.health import build_payload, check, heartbeat_age_sec, heartbeat_loop, read_heartbeat, write_heartbeat
from src.telegram.client import ChannelRef
from src.telegram.listener import ChannelListener

from .test_listener import PEER, FakeTg, ScriptedPipeline

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def beat(tmp_path, *, ts_ago=10, poll_ago=None, started_ago=10, interval=300):
    p = tmp_path / "heartbeat.json"
    write_heartbeat(p, {
        "ts": (NOW - timedelta(seconds=ts_ago)).isoformat(),
        "started_at": (NOW - timedelta(seconds=started_ago)).isoformat(),
        "last_success_poll_at": None if poll_ago is None else (NOW - timedelta(seconds=poll_ago)).isoformat(),
        "poll_interval_sec": interval, "poll_failures": 0, "pid": 1, "version": "x"})
    return p


def test_fresh_ok(tmp_path):
    ok, reason = check(beat(tmp_path, poll_ago=100), NOW)
    assert ok, reason


def test_stale_heartbeat(tmp_path):
    ok, reason = check(beat(tmp_path, ts_ago=181, poll_ago=100), NOW)
    assert not ok and "stale" in reason
    assert check(beat(tmp_path, ts_ago=179, poll_ago=100), NOW)[0]


def test_stale_last_poll(tmp_path):
    limit = 2 * 300 + 600
    assert check(beat(tmp_path, poll_ago=limit - 5), NOW)[0]
    ok, reason = check(beat(tmp_path, poll_ago=limit + 5), NOW)
    assert not ok and "successful poll" in reason


def test_no_poll_yet_within_grace_and_after(tmp_path):
    assert check(beat(tmp_path, started_ago=300 + 600 - 5), NOW)[0]
    ok, reason = check(beat(tmp_path, ts_ago=10, started_ago=300 + 600 + 5), NOW)
    assert not ok and "no successful poll" in reason


def test_missing_or_corrupt_file(tmp_path):
    assert not check(tmp_path / "none.json", NOW)[0]
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert not check(bad, NOW)[0]
    assert heartbeat_age_sec(bad, NOW) is None


def test_write_is_atomic_and_readable(tmp_path):
    p = tmp_path / "sub" / "hb.json"
    write_heartbeat(p, {"a": 1})
    assert read_heartbeat(p) == {"a": 1}
    assert not list(p.parent.glob("*.tmp"))


def test_build_payload_fields():
    listener = SimpleNamespace(last_poll_at=NOW, poll_interval=120, poll_failures=2)
    d = build_payload(listener, NOW - timedelta(hours=1), NOW)
    assert d["version"] == __version__ and d["poll_failures"] == 2 and d["poll_interval_sec"] == 120
    assert d["last_poll_at"] == NOW.isoformat() and d["pid"] > 0
    assert build_payload(SimpleNamespace(last_poll_at=None, poll_interval=60, poll_failures=0), NOW)["last_poll_at"] is None


async def test_heartbeat_loop_writes_and_alerts_on_poll_failures(tmp_path):
    sent = []

    async def send(text):
        sent.append(text)

    listener = SimpleNamespace(last_poll_at=None, poll_interval=60, poll_failures=3,
                               last_poll_error="@chan: OSError: <boom>")
    path = tmp_path / "hb.json"
    task = asyncio.create_task(heartbeat_loop(path, listener, NOW, alerter=Alerter(send), interval=0.01))
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert read_heartbeat(path)["poll_failures"] == 3
    assert len(sent) == 1 and "&lt;boom&gt;" in sent[0] and "3" in sent[0]  # cooldown: one alert


async def test_heartbeat_loop_survives_write_errors(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")  # parent of the heartbeat path is a file: every write fails
    task = asyncio.create_task(heartbeat_loop(blocker / "hb.json", SimpleNamespace(), NOW, interval=0.01))
    await asyncio.sleep(0.05)
    assert not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_cli_exit_codes(tmp_path):
    ini = tmp_path / "c.ini"
    hb = tmp_path / "hb.json"
    ini.write_text(f"[paths]\nheartbeat_file = {hb.as_posix()}\n", encoding="utf-8")
    # the CLI reads PROJECT_ROOT/config.ini: point it to a file via a throwaway module call instead
    code = (
        "import sys; from pathlib import Path; from src import health; from src.config import load_settings;"
        f"s = load_settings(ini_file=Path(r'{ini}'));"
        "ok, r = health.check(s.heartbeat_file); print(ok, r); sys.exit(0 if ok else 1)")
    assert subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT, capture_output=True).returncode == 1
    write_heartbeat(hb, build_payload(SimpleNamespace(last_success_poll_at=datetime.now(timezone.utc), poll_interval=300,
                                                      poll_failures=0), datetime.now(timezone.utc)))
    assert subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT, capture_output=True).returncode == 0


def test_health_module_does_not_import_telethon_or_aiogram():
    code = "import sys, src.health; sys.exit(1 if {'telethon', 'aiogram'} & set(sys.modules) else 0)"
    assert subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT).returncode == 0


def test_cli_usage_error():
    assert subprocess.run([sys.executable, "-m", "src.health"], cwd=PROJECT_ROOT,
                          capture_output=True).returncode == 2


# ------------------------------------------------------------------ listener.poll_failures

class FailingTg(FakeTg):
    def __init__(self):
        super().__init__([])
        self.fail = True

    async def fetch_new(self, ref, after_id, limit):
        if self.fail:
            raise OSError("network down")
        return []


async def test_poll_failures_count_and_reset(repo):
    await repo.upsert_channel(PEER, "chan", "Chan")
    tg = FailingTg()
    listener = ChannelListener(tg, ScriptedPipeline(), repo, [], catchup_limit=50)
    listener.refs = [ChannelRef("chan", PEER, object(), "Chan")]
    for expected in (1, 2, 3):
        await listener._poll_tracked()
        assert listener.poll_failures == expected
    assert "network down" in listener.last_poll_error and listener.last_poll_at is not None
    tg.fail = False
    await listener._poll_tracked()
    assert listener.poll_failures == 0 and listener.last_poll_error is None


async def test_poll_with_no_channels_is_not_a_failure(repo):
    listener = ChannelListener(FakeTg([]), ScriptedPipeline(), repo, [], catchup_limit=50)
    await listener._poll_tracked()
    assert listener.poll_failures == 0


async def test_all_failing_cycles_make_healthcheck_unhealthy_until_a_success(repo, tmp_path):
    await repo.upsert_channel(PEER, "chan", "Chan")
    tg = FailingTg()
    listener = ChannelListener(tg, ScriptedPipeline(), repo, [], catchup_limit=50, poll_interval_sec=300)
    listener.refs = [ChannelRef("chan", PEER, object(), "Chan")]
    started = datetime.now(timezone.utc)
    await listener._poll_tracked()
    assert listener.last_poll_at is not None and listener.last_success_poll_at is None
    hb = tmp_path / "hb.json"
    later = started + timedelta(seconds=300 + 600 + 5)
    write_heartbeat(hb, build_payload(listener, started, later))
    ok, reason = check(hb, later)
    assert not ok and "successful poll" in reason  # last_poll_at keeps moving, health does not
    tg.fail = False
    await listener._poll_tracked()
    assert listener.last_success_poll_at is not None
    write_heartbeat(hb, build_payload(listener, started))
    assert check(hb)[0]
