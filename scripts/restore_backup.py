"""Restore the database (and optionally the Telegram session) from a backup.

Usage:
    python scripts/restore_backup.py --list
    python scripts/restore_backup.py <backup-file.db> [--session <file.session>] [--force]

Stop the service first (docker compose stop). The script refuses to run while the heartbeat
is fresh (younger than 180 s) unless --force is given. The current data/app.db is copied to
data/app.db.before-restore-<timestamp> (raw file copy, plus -wal/-shm/-journal) before it is replaced.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backup import list_backups, session_file_path, sqlite_copy, sqlite_path  # noqa: E402
from src.config import load_settings  # noqa: E402
from src.health import MAX_HEARTBEAT_AGE_SEC, heartbeat_age_sec  # noqa: E402


def _quick_check(db: Path) -> str:
    conn = sqlite3.connect(str(db))
    try:
        rows = [str(r[0]) for r in conn.execute("PRAGMA quick_check").fetchall()]
    except sqlite3.DatabaseError as e:  # not a database at all
        return f"{type(e).__name__}: {e}"
    finally:
        conn.close()
    return "ok" if rows == ["ok"] else "; ".join(rows)


SIDECARS = ("-wal", "-shm", "-journal")


def _clear_wal(db: Path) -> None:
    """Stale -wal/-shm/-journal files of the replaced DB must not be applied to the restored one."""
    for suffix in SIDECARS:
        Path(str(db) + suffix).unlink(missing_ok=True)


def _raw_save(current: Path, stamp: str) -> Path | None:
    """Raw file copy (no sqlite, works on a corrupted file) of ``current`` and its existing
    -wal/-shm/-journal sidecars to ``<name>.before-restore-<stamp>`` (+ the same suffixes).
    The app is stopped at this point. A failure is reported but never blocks the restore."""
    saved = current.with_name(f"{current.name}.before-restore-{stamp}")
    try:
        shutil.copy2(current, saved)
        for suffix in SIDECARS:
            side = Path(str(current) + suffix)
            if side.exists():
                shutil.copy2(side, Path(str(saved) + suffix))
    except OSError as e:
        print(f"warning: cannot save {current} before restore: {e}")
        return None
    return saved


def restore(backup: Path, db_path: Path, *, session: Path | None = None, session_path: Path | None = None,
            heartbeat_file: Path | None = None, force: bool = False,
            now: datetime | None = None) -> int:
    """Returns the exit code (0 ok, 1 refused/failed)."""
    now = now or datetime.now(timezone.utc)
    backup = Path(backup)
    if not backup.is_file():
        print(f"backup file not found: {backup}")
        return 1
    if heartbeat_file is not None and not force:
        age = heartbeat_age_sec(heartbeat_file, now)
        if age is not None and age < MAX_HEARTBEAT_AGE_SEC:
            print(f"the service seems to be running (heartbeat {int(age)} s old). "
                  "Stop it first (docker compose stop) or use --force")
            return 1
    check = _quick_check(backup)
    if check != "ok":
        print(f"the backup itself is damaged (quick_check: {check}), nothing changed")
        return 1
    stamp = now.strftime("%Y%m%d-%H%M%S")
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        saved = _raw_save(db_path, stamp)
        if saved is not None:
            print(f"current database saved to {saved}")
    _clear_wal(db_path)
    sqlite_copy(backup, db_path)
    result = _quick_check(db_path)
    print(f"database restored from {backup.name}; quick_check: {result}")
    code = 0 if result == "ok" else 1
    if session is not None:
        if session_path is None:
            print("session path is unknown, session not restored")
            return 1
        target = session_file_path(session_path)
        if target.exists():
            saved = _raw_save(target, stamp)
            if saved is not None:
                print(f"current session saved to {saved}")
        _clear_wal(target)
        sqlite_copy(Path(session), target)
        print(f"session restored from {Path(session).name}")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore the DB (and session) from a backup")
    parser.add_argument("backup", nargs="?", help="backup file app-*.db")
    parser.add_argument("--list", action="store_true", help="list available backups")
    parser.add_argument("--session", help="session backup file session-*.session to restore too")
    parser.add_argument("--force", action="store_true", help="restore even if the service seems running")
    args = parser.parse_args(argv)

    s = load_settings()
    if args.list:
        items = list_backups(s.backup_dir)
        if not items:
            print(f"no backups in {s.backup_dir}")
        for path, size, mtime in items:
            print(f"{mtime:%Y-%m-%d %H:%M:%S} UTC  {size / 1024:9.1f} KiB  {path}")
        return 0
    if not args.backup:
        parser.print_usage()
        return 2
    db_path = sqlite_path(s.database_url)
    if db_path is None:
        print("DATABASE_URL is not a sqlite file, nothing to restore")
        return 1
    return restore(Path(args.backup), db_path, session=Path(args.session) if args.session else None,
                   session_path=s.session_file, heartbeat_file=s.heartbeat_file, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
