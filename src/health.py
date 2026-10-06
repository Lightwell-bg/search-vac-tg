"""Heartbeat file + Docker healthcheck.

The running app writes ``data/heartbeat.json`` every minute (``heartbeat_loop``);
``python -m src.health check`` (Docker HEALTHCHECK) reads it and exits 0 (healthy) or 1.
Keep this module light: no telethon/aiogram imports.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .alerts import KIND_POLL_FAILING, clip_escape

log = logging.getLogger("health")

HEARTBEAT_INTERVAL_SEC = 60
MAX_HEARTBEAT_AGE_SEC = 180          # heartbeat file older than this = process hung/dead
POLL_GRACE_SEC = 600                 # slack on top of the poll interval
POLL_FAILING_THRESHOLD = 3           # consecutive failed poll cycles that trigger an alert


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def build_payload(listener, started_at: datetime, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    return {
        "ts": _iso(now),
        "started_at": _iso(started_at),
        "pid": os.getpid(),
        "version": __version__,
        "last_poll_at": _iso(getattr(listener, "last_poll_at", None)),
        "last_success_poll_at": _iso(getattr(listener, "last_success_poll_at", None)),
        "poll_interval_sec": int(getattr(listener, "poll_interval", 0) or 0),
        "poll_failures": int(getattr(listener, "poll_failures", 0) or 0),
    }


def write_heartbeat(path: Path, payload: dict) -> None:
    """Atomic write: tmp file in the same directory + os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def read_heartbeat(path: Path) -> dict | None:
    """The heartbeat dict, or None if the file is missing/corrupt."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _parse(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def heartbeat_age_sec(path: Path, now: datetime | None = None) -> float | None:
    """Seconds since the last heartbeat, None if there is none (usable for a bot status line)."""
    ts = _parse((read_heartbeat(path) or {}).get("ts"))
    if ts is None:
        return None
    return ((now or datetime.now(timezone.utc)) - ts).total_seconds()


def check(path: Path, now: datetime | None = None) -> tuple[bool, str]:
    """(healthy, reason)."""
    now = now or datetime.now(timezone.utc)
    data = read_heartbeat(path)
    if data is None:
        return False, f"heartbeat file {path} is missing or unreadable"
    ts = _parse(data.get("ts"))
    if ts is None:
        return False, "heartbeat has no valid ts"
    age = (now - ts).total_seconds()
    if age > MAX_HEARTBEAT_AGE_SEC:
        return False, f"heartbeat is stale: {int(age)} s old (limit {MAX_HEARTBEAT_AGE_SEC} s)"
    try:
        interval = int(data.get("poll_interval_sec") or 300)
    except (TypeError, ValueError):
        interval = 300
    # last_success_poll_at: a cycle where every channel failed does not count as a poll
    last_poll = _parse(data.get("last_success_poll_at"))
    if last_poll is None:
        started = _parse(data.get("started_at")) or ts
        up = (now - started).total_seconds()
        if up >= interval + POLL_GRACE_SEC:
            return False, f"no successful poll since start ({int(up)} s ago)"
        return True, "ok (no poll yet, within grace period)"
    poll_age = (now - last_poll).total_seconds()
    limit = 2 * interval + POLL_GRACE_SEC
    if poll_age >= limit:
        return False, f"last successful poll finished {int(poll_age)} s ago (limit {limit} s)"
    return True, "ok"


async def heartbeat_loop(path: Path, listener, started_at: datetime, *, alerter=None,
                         interval: float = HEARTBEAT_INTERVAL_SEC) -> None:
    """Write the heartbeat forever and raise the ``poll_failing`` alert. Never crashes."""
    while True:
        try:
            write_heartbeat(path, build_payload(listener, started_at))
        except Exception:
            log.exception("cannot write heartbeat")
        try:
            failures = int(getattr(listener, "poll_failures", 0) or 0)
            if alerter is not None and failures >= POLL_FAILING_THRESHOLD:
                err = getattr(listener, "last_poll_error", None) or ""
                await alerter.alert(KIND_POLL_FAILING,
                                    f"⚠️ Опрос каналов не работает: сбоев подряд — {failures}. "
                                    f"Последняя ошибка: {clip_escape(err)}")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("heartbeat alert check failed")
        await asyncio.sleep(interval)


def cli(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] != "check":
        print("usage: python -m src.health check", file=sys.stderr)
        return 2
    from .config import load_settings
    ok, reason = check(load_settings().heartbeat_file)
    if not ok:
        print(f"UNHEALTHY: {reason}")
        return 1
    print(f"healthy: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv))
