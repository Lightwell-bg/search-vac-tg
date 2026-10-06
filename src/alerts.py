"""Owner alerts: short Telegram messages about the bot's own health.

``Alerter.alert`` never raises. Alerts of the same kind are rate-limited by a cooldown
(startup/shutdown are not). ``record_error`` counts external-service errors and raises
an alert once the threshold is crossed (>= 5 errors within 30 minutes by default).
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger("alerts")

KIND_STARTUP = "startup"
KIND_SHUTDOWN = "shutdown"
KIND_POLL_FAILING = "poll_failing"
KIND_JEV_ERRORS = "jev_errors"
KIND_OPENROUTER_ERRORS = "openrouter_errors"
KIND_BACKUP_FAILED = "backup_failed"
KIND_DB_INTEGRITY = "db_integrity"

BYPASS_COOLDOWN = (KIND_STARTUP, KIND_SHUTDOWN)
ERROR_KINDS = {
    KIND_JEV_ERRORS: "JEV",
    KIND_OPENROUTER_ERRORS: "OpenRouter",
}
ERROR_THRESHOLD = 5
ERROR_WINDOW_SEC = 30 * 60
MAX_DETAIL_CHARS = 300


def clip_escape(text: object, limit: int = MAX_DETAIL_CHARS) -> str:
    """Clip untrusted text (error messages of external services) and make it HTML-safe."""
    s = str(text or "")
    s = s if len(s) <= limit else s[:limit] + "…"
    return html.escape(s)


class Alerter:
    def __init__(self, send: Callable[[str], Awaitable], *, cooldown_sec: float = 3600,
                 clock: Callable[[], float] = time.monotonic,
                 enabled: Callable[[], bool] = lambda: True,
                 error_threshold: int = ERROR_THRESHOLD, error_window_sec: float = ERROR_WINDOW_SEC) -> None:
        self._send = send
        self.cooldown_sec = cooldown_sec
        self._clock = clock
        self._enabled = enabled
        self.error_threshold = error_threshold
        self.error_window_sec = error_window_sec
        self._last_sent: dict[str, float] = {}
        self._errors: dict[str, deque[float]] = {}
        self._tasks: set[asyncio.Task] = set()

    def enabled(self) -> bool:
        try:
            return bool(self._enabled())
        except Exception:
            log.exception("alerts enabled() failed")
            return False

    async def alert(self, kind: str, text: str) -> bool:
        """Send ``text`` (HTML) to the owner. True if it was sent. Never raises."""
        try:
            if not self.enabled():
                return False
            now = self._clock()
            if kind not in BYPASS_COOLDOWN:
                last = self._last_sent.get(kind)
                if last is not None and now - last < self.cooldown_sec:
                    return False
            # stamped before the await: concurrent callers of the same kind do not double-send,
            # and a failing Telegram is not retried on every call
            self._last_sent[kind] = now
            await self._send(text)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("cannot send alert %s: %s", kind, e)
            return False

    def record_error(self, kind: str, detail: object = "") -> bool:
        """Count an error of ``kind``; when the threshold is crossed schedule an alert.
        Sync (called from pipeline code). Returns True if the threshold is reached."""
        now = self._clock()
        q = self._errors.setdefault(kind, deque())
        q.append(now)
        while q and now - q[0] > self.error_window_sec:
            q.popleft()
        if len(q) < self.error_threshold:
            return False
        name = ERROR_KINDS.get(kind, kind)
        text = (f"⚠️ {html.escape(name)}: {len(q)} ошибок за {int(self.error_window_sec // 60)} мин."
                f" Последняя: {clip_escape(detail)}")
        try:
            task = asyncio.get_running_loop().create_task(self.alert(kind, text))
        except RuntimeError:
            return True  # no running loop (sync context): nothing to send with
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True


async def send_emergency_alert(settings, text: str, marker_path: Path | str, *,
                               min_interval_sec: float = 21600, now: float | None = None) -> bool:
    """Send ``text`` (HTML) to the owner through a short-lived bot built from the .env settings
    (NOTIFY_BOT_TOKEN / OWNER_TELEGRAM_ID), used when the DB (and so the normal bot) is unusable.

    Rate-limited by a marker file (``{"sent_at": <epoch>}``): docker restarts the crashed app in a
    loop, so at most one message per ``min_interval_sec``. True if sent. Never raises."""
    marker = Path(marker_path)
    now = time.time() if now is None else now
    try:
        try:
            last = float(json.loads(marker.read_text(encoding="utf-8")).get("sent_at", 0))
        except (OSError, ValueError, AttributeError, TypeError):
            last = None
        if last is not None and 0 <= now - last < min_interval_sec:
            return False
        token = getattr(settings, "notify_bot_token", "")
        owner = getattr(settings, "owner_telegram_id", None)
        if not token or owner is None:
            log.warning("emergency alert not sent: NOTIFY_BOT_TOKEN/OWNER_TELEGRAM_ID not set")
            return False
        from aiogram import Bot  # lazy: src.health imports this module and must stay light
        from aiogram.enums import ParseMode
        bot = Bot(token)
        try:
            await bot.send_message(owner, text, parse_mode=ParseMode.HTML)
        finally:
            await bot.session.close()
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"sent_at": now}), encoding="utf-8")
        except OSError:
            log.warning("cannot write the emergency alert marker %s", marker)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("cannot send the emergency alert: %s", e)
        return False
