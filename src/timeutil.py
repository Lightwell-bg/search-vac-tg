"""Local-time helpers: the DB stores UTC, the owner sees the configured timezone."""
from __future__ import annotations

import logging
from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo

log = logging.getLogger("timeutil")

DEFAULT_FMT = "%d.%m %H:%M"


def get_tz(tz: str | tzinfo | None) -> tzinfo:
    """Resolve an IANA name (or tzinfo); unknown names fall back to UTC with a warning."""
    if isinstance(tz, tzinfo):
        return tz
    try:
        return ZoneInfo(tz or "UTC")
    except Exception:
        log.warning("unknown timezone %r, using UTC", tz)
        return timezone.utc


def to_local(dt: datetime, tz: str | tzinfo | None) -> datetime:
    """Convert to the local timezone; a naive datetime is taken as UTC (SQLite drops tzinfo)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(get_tz(tz))


def fmt_local(dt: datetime, tz: str | tzinfo | None, fmt: str = DEFAULT_FMT) -> str:
    return to_local(dt, tz).strftime(fmt)
