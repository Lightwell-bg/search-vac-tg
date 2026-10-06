"""Online backups of the SQLite DB and the Telethon session (sqlite3 backup API, not a file copy).

``BackupService`` runs a daily backup at a local hour and a manual ``backup_now`` (used by the
bot); both are serialized by one lock. Files: ``app-YYYYmmdd-HHMMSS.db`` and
``session-YYYYmmdd-HHMMSS.session`` in the backup dir, at most ``keep`` of each kind.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Callable

from .alerts import KIND_BACKUP_FAILED, clip_escape
from .timeutil import get_tz

log = logging.getLogger("backup")

DB_PREFIX, DB_SUFFIX = "app-", ".db"
SESSION_PREFIX, SESSION_SUFFIX = "session-", ".session"
TS_FORMAT = "%Y%m%d-%H%M%S"
MAX_SLEEP_SEC = 60  # the scheduler re-reads the live timezone/hour at least this often


def sqlite_path(database_url: str) -> Path | None:
    """File path of a sqlite(+aiosqlite) URL, None for in-memory/other databases."""
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if database_url.startswith(prefix):
            rest = database_url[len(prefix):]
            return None if not rest or rest == ":memory:" else Path(rest)
    return None


def session_file_path(session_path: Path | str) -> Path:
    """Telethon appends ``.session`` to the configured session name."""
    p = Path(session_path)
    return p if p.suffix == SESSION_SUFFIX else p.with_name(p.name + SESSION_SUFFIX)


def sqlite_copy(src: Path, dst: Path, *, timeout: float = 30.0) -> None:
    """Consistent copy of a (possibly live, WAL) SQLite file through the online backup API.
    Written to a temp name and moved into place with os.replace."""
    if not Path(src).is_file():  # sqlite3.connect would silently create an empty database
        raise FileNotFoundError(f"database file not found: {src}")
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.unlink(missing_ok=True)
    s = sqlite3.connect(str(src), timeout=timeout)
    try:
        d = sqlite3.connect(str(tmp))
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()
    os.replace(tmp, dst)


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:  # Windows / exotic filesystems
        pass


def _free_name(directory: Path, prefix: str, stamp: str, suffix: str) -> Path:
    """app-<stamp>.db, or app-<stamp>_N.db if two backups fall into the same second."""
    p = directory / f"{prefix}{stamp}{suffix}"
    n = 0
    while p.exists():
        n += 1
        p = directory / f"{prefix}{stamp}_{n}{suffix}"
    return p


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _rotate(directory: Path, prefix: str, suffix: str, keep: int, newest: Path | None = None) -> None:
    """Keep ``keep`` files: the just-created ``newest`` (never deleted, even if the clock went
    backwards and its name sorts oldest) plus the ``keep - 1`` most recently modified others."""
    others = [p for p in directory.glob(f"{prefix}*{suffix}") if p.is_file() and p != newest]
    others.sort(key=lambda p: (_mtime(p), p.name), reverse=True)  # newest first
    keep_others = max(0, keep - 1) if newest is not None else keep
    for old in others[keep_others:]:
        try:
            old.unlink()
        except OSError:
            log.warning("cannot delete old backup %s", old)


def _make_backup_sync(db_path: Path, session_path: Path | None, backup_dir: Path, keep: int,
                      now: datetime, warnings: list[str] | None = None) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime(TS_FORMAT)
    db_dst = _free_name(backup_dir, DB_PREFIX, stamp, DB_SUFFIX)
    sqlite_copy(Path(db_path), db_dst)
    _chmod_private(db_dst)
    sess_dst: Path | None = None
    if session_path is not None:
        sess = session_file_path(session_path)
        if sess.exists():
            try:
                sess_dst = _free_name(backup_dir, SESSION_PREFIX, stamp, SESSION_SUFFIX)
                sqlite_copy(sess, sess_dst)
                _chmod_private(sess_dst)
            except Exception as e:  # the DB backup is already safe: a session problem must not lose it
                sess_dst = None
                log.exception("ERROR backing up the Telegram session")
                if warnings is not None:
                    warnings.append(f"сессия не сохранена: {type(e).__name__}: {e}")
        else:
            log.info("no session file %s, skipping its backup", sess)
    keep = max(1, int(keep))
    _rotate(backup_dir, DB_PREFIX, DB_SUFFIX, keep, db_dst)
    _rotate(backup_dir, SESSION_PREFIX, SESSION_SUFFIX, keep, sess_dst)
    return db_dst


async def make_backup(db_path: Path, session_path: Path | None, backup_dir: Path, keep: int, *,
                      now: datetime | None = None, warnings: list[str] | None = None) -> Path:
    """Back up the DB (and the session if it exists) off the event loop; returns the DB backup path.
    Non-fatal problems (the session copy failed) are appended to ``warnings``."""
    return await asyncio.to_thread(_make_backup_sync, Path(db_path),
                                   Path(session_path) if session_path else None, Path(backup_dir), keep,
                                   now or datetime.now(timezone.utc), warnings)


def list_backups(backup_dir: Path) -> list[tuple[Path, int, datetime]]:
    """(path, size in bytes, mtime as aware UTC datetime) of every backup file, newest first."""
    out: list[tuple[Path, int, datetime]] = []
    d = Path(backup_dir)
    if not d.is_dir():
        return out
    for pattern in (f"{DB_PREFIX}*{DB_SUFFIX}", f"{SESSION_PREFIX}*{SESSION_SUFFIX}"):
        for p in d.glob(pattern):
            try:
                st = p.stat()
            except OSError:
                continue
            out.append((p, st.st_size, datetime.fromtimestamp(st.st_mtime, timezone.utc)))
    out.sort(key=lambda x: (x[2], x[0].name), reverse=True)
    return out


def next_backup_time(now: datetime, hour: int, tz: str | tzinfo | None) -> datetime:
    """Next instant (aware UTC) strictly after ``now`` when the local wall clock shows ``hour``:00.
    Computed from local wall time each day, so DST shifts never move the backup off its hour.
    A skipped hour (spring forward) resolves to the instant after the gap, a doubled hour (fall
    back) to its first occurrence."""
    zone = get_tz(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local_now = now.astimezone(zone)
    day = local_now.date()
    for _ in range(3):
        cand = datetime(day.year, day.month, day.day, hour, 0, tzinfo=zone, fold=0).astimezone(timezone.utc)
        if cand > now:
            return cand
        day += timedelta(days=1)
    return cand  # pragma: no cover - unreachable


class BackupService:
    """Daily backup loop + manual ``backup_now``; one lock, so they never run concurrently."""

    def __init__(self, db_path: Path, session_path: Path | None, backup_dir: Path,
                 keep: Callable[[], int] | int, *, alerter=None) -> None:
        self.db_path = Path(db_path)
        self.session_path = Path(session_path) if session_path else None
        self.backup_dir = Path(backup_dir)
        self._keep = keep
        self.alerter = alerter
        self._lock = asyncio.Lock()
        self.last_backup_at: datetime | None = None
        self.last_error: str | None = None

    @property
    def keep(self) -> int:
        return int(self._keep() if callable(self._keep) else self._keep)

    async def backup_now(self) -> Path:
        """Make a backup (waits if another one is running). Raises on failure after alerting."""
        async with self._lock:
            warnings: list[str] = []
            try:
                path = await make_backup(self.db_path, self.session_path, self.backup_dir, self.keep,
                                         warnings=warnings)
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("ERROR backup failed")
                if self.alerter is not None:
                    await self.alerter.alert(
                        KIND_BACKUP_FAILED, f"⚠️ Не удалось сделать бэкап: {clip_escape(self.last_error)}")
                raise
            self.last_error = "; ".join(warnings) if warnings else None
            self.last_backup_at = datetime.now(timezone.utc)
            log.info("backup created: %s", path.name)
            if warnings and self.alerter is not None:  # the DB copy is safe, but the session is not
                await self.alerter.alert(
                    KIND_BACKUP_FAILED, f"⚠️ Бэкап БД сделан, но {clip_escape(self.last_error)}")
            return path

    def list(self) -> list[tuple[Path, int, datetime]]:
        return list_backups(self.backup_dir)

    async def run_loop(self, hour: Callable[[], int] | int, tz: Callable[[], str | None] | str | None, *,
                       clock: Callable[[], datetime] | None = None, sleep=None) -> None:
        """Back up once a day at the local ``hour``. Never crashes; CancelledError propagates.

        The live hour/timezone are re-read on every wake (sleeps are at most MAX_SLEEP_SEC); a backup
        runs when the local ``hour`` has been reached and none has run yet for that local date.
        Starting after the hour does not trigger an immediate backup."""
        clock = clock or (lambda: datetime.now(timezone.utc))
        sleep = sleep or asyncio.sleep
        last_date = None
        first = True
        while True:
            h = hour() if callable(hour) else hour
            zone = get_tz(tz() if callable(tz) else tz)
            now = clock()
            local_date = now.astimezone(zone).date()
            slot = datetime(local_date.year, local_date.month, local_date.day, h, 0, tzinfo=zone,
                            fold=0).astimezone(timezone.utc)
            if first:
                first = False
                if now >= slot:
                    last_date = local_date  # today's slot passed before start: wait for tomorrow's
            if last_date != local_date and now >= slot:
                last_date = local_date
                try:
                    await self.backup_now()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass  # already logged and alerted in backup_now
                continue
            target = slot if last_date != local_date else next_backup_time(now, h, zone)
            remaining = (target - now).total_seconds()
            await sleep(min(max(remaining, 0.01), MAX_SLEEP_SEC))
