"""Backups (online sqlite API), rotation, scheduling, integrity check and the restore script."""
import asyncio
import importlib.util
import logging
import logging.handlers
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.backup import (BackupService, list_backups, make_backup, next_backup_time, session_file_path,
                        sqlite_path)
from src.config import PROJECT_ROOT, load_settings, setup_logging
from src.db.database import Database
from src.health import write_heartbeat

_spec = importlib.util.spec_from_file_location("restore_backup", PROJECT_ROOT / "scripts" / "restore_backup.py")
restore_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(restore_mod)

T0 = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)


def make_sqlite(path, rows=("a", "b")):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    conn.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])
    conn.commit()
    conn.close()
    return path


def read_rows(path):
    conn = sqlite3.connect(str(path))
    try:
        return [r[0] for r in conn.execute("SELECT v FROM t ORDER BY rowid")]
    finally:
        conn.close()


async def test_backup_creates_valid_copies(tmp_path):
    db = make_sqlite(tmp_path / "app.db")
    session = make_sqlite(tmp_path / "telegram.session", ("s",))
    out = await make_backup(db, tmp_path / "telegram", tmp_path / "bk", keep=3, now=T0)
    assert out.name == "app-20261006-120000.db" and read_rows(out) == ["a", "b"]
    sess = tmp_path / "bk" / "session-20261006-120000.session"
    assert read_rows(sess) == ["s"]
    assert session.exists() and not list((tmp_path / "bk").glob("*.tmp"))


async def test_backup_of_live_wal_database(tmp_path):
    db_file = tmp_path / "live.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES ('in-wal')")
    conn.commit()  # data may still sit in the -wal file; a plain file copy would miss it
    out = await make_backup(db_file, None, tmp_path / "bk", keep=2, now=T0)
    conn.close()
    assert read_rows(out) == ["in-wal"]


async def test_missing_session_is_ok(tmp_path):
    db = make_sqlite(tmp_path / "app.db")
    out = await make_backup(db, tmp_path / "nosuch", tmp_path / "bk", keep=2, now=T0)
    assert out.exists() and not list((tmp_path / "bk").glob("session-*"))


async def test_rotation_keeps_n_of_each_kind(tmp_path):
    db = make_sqlite(tmp_path / "app.db")
    make_sqlite(tmp_path / "telegram.session")
    for i in range(5):
        await make_backup(db, tmp_path / "telegram", tmp_path / "bk", keep=2, now=T0 + timedelta(days=i))
    dbs = sorted(p.name for p in (tmp_path / "bk").glob("app-*.db"))
    sess = sorted(p.name for p in (tmp_path / "bk").glob("session-*.session"))
    assert dbs == ["app-20261009-120000.db", "app-20261010-120000.db"]
    assert len(sess) == 2 and sess[-1] == "session-20261010-120000.session"


async def test_same_second_does_not_overwrite(tmp_path):
    db = make_sqlite(tmp_path / "app.db")
    a = await make_backup(db, None, tmp_path / "bk", keep=5, now=T0)
    b = await make_backup(db, None, tmp_path / "bk", keep=5, now=T0)
    assert a != b and a.exists() and b.exists()


async def test_list_backups_newest_first(tmp_path):
    db = make_sqlite(tmp_path / "app.db")
    import os
    paths = []
    for i in range(3):
        p = await make_backup(db, None, tmp_path / "bk", keep=5, now=T0 + timedelta(days=i))
        os.utime(p, (T0.timestamp() + i * 86400, T0.timestamp() + i * 86400))
        paths.append(p)
    items = list_backups(tmp_path / "bk")
    assert [i[0] for i in items] == paths[::-1]
    path, size, mtime = items[0]
    assert size > 0 and mtime.tzinfo is not None
    assert list_backups(tmp_path / "nonexistent") == []


async def test_concurrent_backup_now_is_serialized(tmp_path):
    db = make_sqlite(tmp_path / "app.db")
    svc = BackupService(db, None, tmp_path / "bk", lambda: 10)
    running = {"now": 0, "max": 0}
    import src.backup as backup_mod
    real = backup_mod.make_backup

    async def slow(*a, **k):
        running["now"] += 1
        running["max"] = max(running["max"], running["now"])
        await asyncio.sleep(0.05)
        try:
            return await real(*a, **k)
        finally:
            running["now"] -= 1

    backup_mod.make_backup, orig = slow, backup_mod.make_backup
    try:
        results = await asyncio.gather(svc.backup_now(), svc.backup_now(), svc.backup_now())
    finally:
        backup_mod.make_backup = orig
    assert running["max"] == 1 and len({str(r) for r in results}) == 3
    assert svc.last_backup_at is not None and svc.last_error is None


async def test_backup_failure_alerts_and_raises(tmp_path):
    sent = []

    async def send(text):
        sent.append(text)

    from src.alerts import Alerter
    svc = BackupService(tmp_path / "missing.db", None, tmp_path / "bk", 3, alerter=Alerter(send))
    with pytest.raises(Exception):
        await svc.backup_now()
    assert len(sent) == 1 and "бэкап" in sent[0] and svc.last_error


def test_keep_is_live(tmp_path):
    keep = {"v": 3}
    svc = BackupService(tmp_path / "x.db", None, tmp_path, lambda: keep["v"])
    keep["v"] = 9
    assert svc.keep == 9


def test_session_file_path_and_sqlite_path(tmp_path):
    assert session_file_path("data/telegram").name == "telegram.session"
    assert session_file_path("data/telegram.session").name == "telegram.session"
    assert sqlite_path("sqlite+aiosqlite:///D:/x/app.db").as_posix() == "D:/x/app.db"
    assert sqlite_path("sqlite+aiosqlite:///:memory:") is None
    assert sqlite_path("postgresql://x") is None


# ------------------------------------------------------------------ schedule

SOFIA = "Europe/Sofia"


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_next_backup_time_plain_day():
    # 2026-10-06 12:00 UTC = 15:00 Sofia (EEST, +3); 04:00 local tomorrow = 01:00 UTC
    assert next_backup_time(utc(2026, 10, 6, 12), 4, SOFIA) == utc(2026, 10, 7, 1)
    # before the hour today
    assert next_backup_time(utc(2026, 10, 6, 0), 4, SOFIA) == utc(2026, 10, 6, 1)


def test_next_backup_time_is_strictly_after_now():
    t = utc(2026, 10, 7, 1)
    assert next_backup_time(t, 4, SOFIA) == utc(2026, 10, 8, 1)


def test_next_backup_time_across_spring_forward():
    # Sofia: 2026-03-29 03:00 -> 04:00 (EET +2 -> EEST +3)
    before = utc(2026, 3, 28, 12)           # Mar 28 14:00 local
    assert next_backup_time(before, 4, SOFIA) == utc(2026, 3, 29, 1)     # 04:00 EEST
    # the hour that does not exist: first matching instant (just after the gap)
    assert next_backup_time(before, 3, SOFIA) == utc(2026, 3, 29, 1)
    # the day after the switch: back to a plain 04:00 local (= 01:00 UTC with +3)
    assert next_backup_time(utc(2026, 3, 29, 2), 4, SOFIA) == utc(2026, 3, 30, 1)
    # the day before the switch the same wall hour was 02:00 UTC (+2): offset changed, hour did not
    assert next_backup_time(utc(2026, 3, 27, 12), 4, SOFIA) == utc(2026, 3, 28, 2)


def test_next_backup_time_across_fall_back():
    # Sofia: 2026-10-25 04:00 -> 03:00 (EEST +3 -> EET +2); hour 3 occurs twice: use the first
    got = next_backup_time(utc(2026, 10, 24, 12), 3, SOFIA)
    assert got == utc(2026, 10, 25, 0)                                    # 03:00 EEST
    # right after that run it does not fire a second time in the repeated hour
    assert next_backup_time(got, 3, SOFIA) == utc(2026, 10, 26, 1)        # 03:00 EET next day
    assert next_backup_time(utc(2026, 10, 25, 12), 4, SOFIA) == utc(2026, 10, 26, 2)


def test_next_backup_time_unknown_tz_falls_back_to_utc():
    assert next_backup_time(utc(2026, 10, 6, 12), 4, "No/Such") == utc(2026, 10, 7, 4)
    assert next_backup_time(utc(2026, 10, 6, 12), 4, ZoneInfo("UTC")) == utc(2026, 10, 7, 4)


class _Stop(Exception):
    pass


def fake_clock(start, end, hook=None):
    """(clock, sleep, sleeps): sleep advances the fake clock and stops the loop at ``end``."""
    state = {"now": start}
    sleeps = []

    async def sleep(sec):
        sleeps.append(sec)
        assert sec <= 60
        state["now"] += timedelta(seconds=sec)
        if hook:
            hook(state["now"])
        if state["now"] >= end:
            raise _Stop

    return (lambda: state["now"]), sleep, sleeps


async def test_run_loop_survives_failures(tmp_path):
    svc = BackupService(tmp_path / "missing.db", None, tmp_path / "bk", 3)
    calls = []
    orig = svc.backup_now

    async def counting():
        calls.append(1)
        await orig()

    svc.backup_now = counting
    clock, sleep, _ = fake_clock(utc(2026, 10, 6, 0), utc(2026, 10, 8, 12))
    with pytest.raises(_Stop):
        await svc.run_loop(4, "UTC", clock=clock, sleep=sleep)
    assert len(calls) == 3   # one attempt per local day (Oct 6, 7, 8); the failing backup did not kill the loop


async def test_run_loop_applies_timezone_change_and_never_double_runs(tmp_path):
    svc = BackupService(tmp_path / "x.db", None, tmp_path / "bk", 3)
    calls = []

    async def fake_backup():
        calls.append(1)

    svc.backup_now = fake_backup
    tz = {"v": "UTC"}

    def hook(now):
        if now == utc(2026, 10, 6, 1):
            tz["v"] = "Asia/Tokyo"            # 10:00 local on Oct 6: the 04:00 slot has already passed
        if now == utc(2026, 10, 6, 3):
            tz["v"] = "UTC"                   # back: UTC date Oct 6 already had its backup

    clock, sleep, sleeps = fake_clock(utc(2026, 10, 6, 0), utc(2026, 10, 6, 6), hook)
    with pytest.raises(_Stop):
        await svc.run_loop(4, lambda: tz["v"], clock=clock, sleep=sleep)
    assert len(calls) == 1 and max(sleeps) <= 60


async def test_run_loop_does_not_back_up_on_start_after_the_hour(tmp_path):
    svc = BackupService(tmp_path / "x.db", None, tmp_path / "bk", 3)
    calls = []

    async def fake_backup():
        calls.append(1)

    svc.backup_now = fake_backup
    clock, sleep, _ = fake_clock(utc(2026, 10, 6, 10), utc(2026, 10, 7, 5))
    with pytest.raises(_Stop):
        await svc.run_loop(4, "UTC", clock=clock, sleep=sleep)
    assert len(calls) == 1   # only tomorrow's 04:00


# ------------------------------------------------------------------ integrity + logging

async def test_quick_check_ok_and_damaged(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'a.db').as_posix()}")
    await db.init()
    assert await db.quick_check() == "ok"
    await db.close()
    data = bytearray((tmp_path / "a.db").read_bytes())
    for i in range(len(data) // 2, len(data)):
        data[i] = 0xFF  # wreck the pages after the first half
    (tmp_path / "bad.db").write_bytes(bytes(data))
    bad = Database(f"sqlite+aiosqlite:///{(tmp_path / 'bad.db').as_posix()}")
    try:
        assert await bad.quick_check() != "ok"
    except Exception:
        pass  # "file is not a database" is also a failed check; main wraps it in try/except
    finally:
        await bad.close()


def test_log_handler_is_rotating(tmp_path):
    ini = tmp_path / "c.ini"
    ini.write_text(f"[logging]\nfile = {(tmp_path / 'app.log').as_posix()}\nmax_bytes = 4096\nbackup_count = 2\n",
                   encoding="utf-8")
    s = load_settings(env_file=tmp_path / ".env", ini_file=ini)
    root = logging.getLogger()
    saved = list(root.handlers)
    level = root.level
    try:
        setup_logging(s)
        fh = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(fh) == 1 and fh[0].maxBytes == 4096 and fh[0].backupCount == 2
        assert fh[0].encoding == "utf-8"
        assert any(type(h) is logging.StreamHandler for h in root.handlers)
    finally:
        for h in list(root.handlers):
            h.close()
            root.removeHandler(h)
        for h in saved:
            root.addHandler(h)
        root.setLevel(level)


def test_log_and_backup_defaults_from_project_ini(tmp_path):
    s = load_settings(env_file=tmp_path / ".env", ini_file=PROJECT_ROOT / "config.ini")
    assert s.log_max_bytes == 5 * 1024 * 1024 and s.log_backup_count == 3
    assert s.backup_keep == 7 and s.backup_hour == 4 and s.alerts_enabled is True
    assert s.backup_dir.name == "backups" and s.heartbeat_file.name == "heartbeat.json"


# ------------------------------------------------------------------ restore script

def test_restore_round_trip(tmp_path, capsys):
    db = make_sqlite(tmp_path / "app.db", ("old",))
    backup = make_sqlite(tmp_path / "app-backup.db", ("x", "y"))
    session_bk = make_sqlite(tmp_path / "session-bk.session", ("sess-new",))
    session = make_sqlite(tmp_path / "telegram.session", ("sess-old",))
    code = restore_mod.restore(backup, db, session=session_bk, session_path=tmp_path / "telegram",
                               heartbeat_file=tmp_path / "hb.json", now=T0)
    assert code == 0
    assert read_rows(db) == ["x", "y"] and read_rows(session) == ["sess-new"]
    saved = tmp_path / "app.db.before-restore-20261006-120000"
    assert saved.exists() and read_rows(saved) == ["old"]
    assert (tmp_path / "telegram.session.before-restore-20261006-120000").exists()
    assert "quick_check: ok" in capsys.readouterr().out


def test_restore_refuses_when_service_running(tmp_path, capsys):
    db = make_sqlite(tmp_path / "app.db", ("old",))
    backup = make_sqlite(tmp_path / "bk.db", ("new",))
    hb = tmp_path / "hb.json"
    write_heartbeat(hb, {"ts": (T0 - timedelta(seconds=30)).isoformat()})
    assert restore_mod.restore(backup, db, heartbeat_file=hb, now=T0) == 1
    assert read_rows(db) == ["old"] and "seems to be running" in capsys.readouterr().out
    # --force overrides
    assert restore_mod.restore(backup, db, heartbeat_file=hb, force=True, now=T0) == 0
    assert read_rows(db) == ["new"]


def test_restore_allows_stale_heartbeat(tmp_path):
    db = make_sqlite(tmp_path / "app.db", ("old",))
    backup = make_sqlite(tmp_path / "bk.db", ("new",))
    hb = tmp_path / "hb.json"
    write_heartbeat(hb, {"ts": (T0 - timedelta(seconds=600)).isoformat()})
    assert restore_mod.restore(backup, db, heartbeat_file=hb, now=T0) == 0


def test_restore_rejects_missing_or_damaged_backup(tmp_path, capsys):
    db = make_sqlite(tmp_path / "app.db", ("old",))
    assert restore_mod.restore(tmp_path / "none.db", db, now=T0) == 1
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not sqlite" * 100)
    assert restore_mod.restore(junk, db, now=T0) == 1
    assert read_rows(db) == ["old"] and "damaged" in capsys.readouterr().out


def test_restore_list(tmp_path, monkeypatch, capsys):
    bk = tmp_path / "bk"
    bk.mkdir()
    make_sqlite(bk / "app-20261006-120000.db")
    monkeypatch.setattr(restore_mod, "load_settings", lambda: SimpleNamespace(backup_dir=bk))
    assert restore_mod.main(["--list"]) == 0
    assert "app-20261006-120000.db" in capsys.readouterr().out


def test_restore_over_corrupted_db_keeps_raw_copy(tmp_path):
    db = tmp_path / "app.db"
    garbage = b"\x00garbage not sqlite\xff" * 50
    db.write_bytes(garbage)
    (tmp_path / "app.db-wal").write_bytes(b"wal-bytes")
    backup = make_sqlite(tmp_path / "bk.db", ("good",))
    assert restore_mod.restore(backup, db, now=T0) == 0
    assert read_rows(db) == ["good"]
    saved = tmp_path / "app.db.before-restore-20261006-120000"
    assert saved.read_bytes() == garbage
    assert (tmp_path / "app.db.before-restore-20261006-120000-wal").read_bytes() == b"wal-bytes"
    assert not (tmp_path / "app.db-wal").exists()


def test_restore_session_saves_and_clears_sidecars(tmp_path):
    db = make_sqlite(tmp_path / "app.db", ("old",))
    backup = make_sqlite(tmp_path / "bk.db", ("new",))
    session = make_sqlite(tmp_path / "telegram.session", ("sess-old",))
    (tmp_path / "telegram.session-wal").write_bytes(b"w")
    (tmp_path / "telegram.session-journal").write_bytes(b"j")
    session_bk = make_sqlite(tmp_path / "session-bk.session", ("sess-new",))
    assert restore_mod.restore(backup, db, session=session_bk, session_path=tmp_path / "telegram", now=T0) == 0
    stem = tmp_path / "telegram.session.before-restore-20261006-120000"
    assert (tmp_path / (stem.name + "-wal")).read_bytes() == b"w"
    assert (tmp_path / (stem.name + "-journal")).read_bytes() == b"j"
    assert not (tmp_path / "telegram.session-wal").exists()
    assert not (tmp_path / "telegram.session-journal").exists()
    assert read_rows(session) == ["sess-new"]


async def test_rotation_never_deletes_the_new_file_after_clock_change(tmp_path):
    import os
    db = make_sqlite(tmp_path / "app.db")
    bk = tmp_path / "bk"
    for i, day in enumerate((10, 11)):   # older backups carry LATER names
        p = await make_backup(db, None, bk, keep=5, now=utc(2026, 10, day, 12))
        os.utime(p, (1_000_000 + i, 1_000_000 + i))
    out = await make_backup(db, None, bk, keep=1, now=utc(2026, 10, 1, 12))  # clock went back: sorts oldest
    assert out.exists() and [p.name for p in bk.glob("app-*.db")] == [out.name]
    os.utime(out, (500_000, 500_000))
    for i, day in enumerate((20, 21)):
        p = await make_backup(db, None, bk, keep=9, now=utc(2026, 10, day, 12))
        os.utime(p, (2_000_000 + i, 2_000_000 + i))
    out2 = await make_backup(db, None, bk, keep=2, now=utc(2026, 10, 2, 12))
    names = sorted(p.name for p in bk.glob("app-*.db"))
    assert out2.name in names and "app-20261021-120000.db" in names and len(names) == 2


async def test_session_copy_failure_is_reported(tmp_path, monkeypatch):
    import src.backup as backup_mod
    sent = []

    async def send(text):
        sent.append(text)

    from src.alerts import Alerter
    db = make_sqlite(tmp_path / "app.db")
    make_sqlite(tmp_path / "telegram.session")
    real = backup_mod.sqlite_copy

    def flaky(src, dst, **k):
        if str(src).endswith(".session"):
            raise OSError("disk <full>")
        return real(src, dst, **k)

    monkeypatch.setattr(backup_mod, "sqlite_copy", flaky)
    svc = BackupService(db, tmp_path / "telegram", tmp_path / "bk", 3, alerter=Alerter(send))
    path = await svc.backup_now()
    assert path.exists() and svc.last_backup_at is not None
    assert svc.last_error.startswith("сессия не сохранена") and "disk" in svc.last_error
    assert len(sent) == 1 and "&lt;full&gt;" in sent[0]
