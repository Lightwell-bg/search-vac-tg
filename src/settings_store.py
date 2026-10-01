"""Runtime settings editable from the Telegram bot and stored in the DB.

``RuntimeSettings.load`` starts from the env-based ``Settings`` and overrides each value
that has a row in the ``settings`` table. After the first change made from the bot the DB
value wins over ``.env`` (editing ``.env`` no longer changes that setting). Every change
is validated, persisted and then pushed to live objects through subscribers, so no
restart is needed.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import re
from typing import Any, Callable
from zoneinfo import ZoneInfo

log = logging.getLogger("settings")

MODEL_RE = re.compile(r"^[\w.\-~:]+/[\w.\-~:]+$")
MODEL_MAX_LEN = 100

INT_KEYS = ("notify_score", "high_fit_score")
BOOL_KEYS = ("show_paid_contact", "notifications_paused")
ALL_KEYS = INT_KEYS + BOOL_KEYS + ("openrouter_model", "poll_interval_sec", "log_retention_days", "timezone")
POLL_INTERVALS = (60, 120, 300, 600, 900, 1800, 3600)  # preset buttons only
MIN_POLL_SEC = 60
MAX_POLL_SEC = 86400
POLL_RANGE_ERROR = "Интервал должен быть от 1 минуты до 24 часов (60..86400 секунд)"
MIN_RETENTION_DAYS = 7      # real minimum is max(this, dedup window_days)
MAX_RETENTION_DAYS = 365    # real maximum is max(this, dedup window_days)
TIMEZONE_MAX_LEN = 64
TIMEZONE_ERROR = "Неизвестный часовой пояс. Нужно имя IANA, например Europe/Warsaw или UTC"
INTERVAL_EXAMPLES = "Примеры: 500 (минут), 30м, 90с, 2ч, 1.5ч"
_UNITS = {"с": 1, "сек": 1, "секунд": 1, "секунды": 1, "s": 1, "sec": 1,
          "м": 60, "мин": 60, "минут": 60, "минуты": 60, "m": 60, "min": 60,
          "ч": 3600, "час": 3600, "часа": 3600, "часов": 3600, "h": 3600, "hr": 3600}
_INTERVAL_RE = re.compile(r"^(\d+(?:\.\d+)?)([a-zа-яё]*)$")


def parse_interval(text: str) -> int:
    """'500' (minutes), '30м', '90 сек', '2ч', '1.5h' -> seconds. Russian ValueError otherwise."""
    raw = re.sub(r"\s+", "", str(text or "").lower()).replace(",", ".")
    m = _INTERVAL_RE.match(raw)
    if not m or (m.group(2) and m.group(2) not in _UNITS):
        raise ValueError(f"Не удалось разобрать интервал «{str(text or '').strip()[:40]}». {INTERVAL_EXAMPLES}")
    sec = round(float(m.group(1)) * _UNITS.get(m.group(2), 60))
    if not MIN_POLL_SEC <= sec <= MAX_POLL_SEC:
        raise ValueError(POLL_RANGE_ERROR)
    return sec


def format_interval(sec: int) -> str:
    """60 -> '1 мин', 500*60 -> '8 ч 20 мин', 90 -> '90 с'."""
    sec = int(sec)
    if sec < 3600:
        return f"{sec // 60} мин" if sec % 60 == 0 else f"{sec} с"
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return " ".join([f"{h} ч"] + ([f"{m} мин"] if m else []) + ([f"{s} с"] if s else []))


def retention_min(dedup_window_days: int) -> int:
    """The journal must outlive the dedup window, otherwise reposts would not be matched."""
    return max(MIN_RETENTION_DAYS, int(dedup_window_days))


def retention_max(dedup_window_days: int) -> int:
    return max(MAX_RETENTION_DAYS, int(dedup_window_days))


def retention_range_error(dedup_window_days: int = 14) -> str:
    return (f"Срок хранения журнала должен быть целым числом дней от "
            f"{retention_min(dedup_window_days)} до {retention_max(dedup_window_days)}")


def valid_timezone(value: Any) -> str:
    """IANA name known to zoneinfo (surrounding spaces stripped); Russian ValueError otherwise."""
    name = value.strip() if isinstance(value, str) else ""
    if not name or len(name) > TIMEZONE_MAX_LEN:
        raise ValueError(TIMEZONE_ERROR)
    try:
        ZoneInfo(name)
    except Exception:  # ZoneInfoNotFoundError, ValueError (bad key), OSError
        raise ValueError(TIMEZONE_ERROR) from None
    return name


def _valid_score(key: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(f"Значение {key} должно быть целым числом 0..100")
    if isinstance(value, str) and re.fullmatch(r"\s*\d{1,4}\s*", value):
        value = int(value)
    if not isinstance(value, int) or not 0 <= value <= 100:
        raise ValueError("Порог должен быть целым числом от 0 до 100")
    return value


class RuntimeSettings:
    def __init__(self, repo, *, notify_score: int, high_fit_score: int, show_paid_contact: bool,
                 notifications_paused: bool, openrouter_model: str, poll_interval_sec: int = 300,
                 log_retention_days: int = 30, dedup_window_days: int = 14,
                 timezone: str = "Europe/Sofia") -> None:
        self.repo = repo
        self.notify_score = notify_score
        self.high_fit_score = high_fit_score
        self.show_paid_contact = show_paid_contact
        self.notifications_paused = notifications_paused
        self.openrouter_model = openrouter_model
        self.poll_interval_sec = poll_interval_sec
        self.log_retention_days = log_retention_days
        self.dedup_window_days = dedup_window_days
        self.timezone = timezone   # display timezone: config.ini [ui] default, changed from the bot
        self._listeners: list[Callable[["RuntimeSettings"], Any]] = []
        self._lock = asyncio.Lock()

    @classmethod
    async def load(cls, repo, settings) -> "RuntimeSettings":
        """Defaults come from ``settings`` (env); rows stored in the DB override them."""
        values: dict[str, Any] = {
            "notify_score": settings.notify_score,
            "high_fit_score": settings.high_fit_score,
            "show_paid_contact": settings.show_paid_contact,
            "notifications_paused": False,
            "openrouter_model": settings.openrouter_model,
            "poll_interval_sec": getattr(settings, "poll_interval_sec", 300),
            "log_retention_days": getattr(settings, "log_retention_days", 30),
            "timezone": getattr(settings, "timezone", "Europe/Sofia"),
        }
        dedup_days = getattr(settings, "dedup_window_days", 14)
        min_days, max_days = retention_min(dedup_days), retention_max(dedup_days)
        cfg_days = values["log_retention_days"]
        if isinstance(cfg_days, int) and not min_days <= cfg_days <= max_days:
            values["log_retention_days"] = min(max(cfg_days, min_days), max_days)
            log.warning("config retention_days=%s out of range %s..%s, using %s", cfg_days, min_days,
                        max_days, values["log_retention_days"])
        try:
            values["timezone"] = valid_timezone(values["timezone"])
        except ValueError:
            log.warning("unknown timezone %r in config, using UTC", values["timezone"])
            values["timezone"] = "UTC"
        stored = await repo.get_settings()
        for key in ALL_KEYS:
            if key not in stored:
                continue
            v = stored[key]
            ok = (isinstance(v, bool) if key in BOOL_KEYS
                  else isinstance(v, str) if key in ("openrouter_model", "timezone")
                  else isinstance(v, int) and not isinstance(v, bool))
            if ok and key == "poll_interval_sec" and not MIN_POLL_SEC <= v <= MAX_POLL_SEC:
                ok = False
            if ok and key == "log_retention_days" and not min_days <= v <= max_days:
                ok = False
            if ok and key == "timezone":
                try:
                    v = valid_timezone(v)
                except ValueError:
                    ok = False
            if ok:
                values[key] = v
            else:
                log.warning("ignoring invalid stored setting %s=%r", key, v)
        return cls(repo, dedup_window_days=dedup_days, **values)

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in ALL_KEYS}

    def subscribe(self, callback: Callable[["RuntimeSettings"], Any]) -> None:
        """``callback(runtime_settings)`` (sync or async) runs after every successful change."""
        self._listeners.append(callback)

    def _validate(self, key: str, value: Any) -> Any:
        if key in INT_KEYS:
            value = _valid_score(key, value)
            notify = value if key == "notify_score" else self.notify_score
            high = value if key == "high_fit_score" else self.high_fit_score
            if notify > high:
                raise ValueError("Порог уведомления не может быть выше порога высокого соответствия")
            return value
        if key == "poll_interval_sec":
            if isinstance(value, str) and re.fullmatch(r"\s*\d{1,5}\s*", value):
                value = int(value)
            if isinstance(value, bool) or not isinstance(value, int) or not MIN_POLL_SEC <= value <= MAX_POLL_SEC:
                raise ValueError(POLL_RANGE_ERROR)
            return value
        if key == "log_retention_days":
            if isinstance(value, str) and re.fullmatch(r"\s*\d{1,5}\s*", value):
                value = int(value)
            lo, hi = retention_min(self.dedup_window_days), retention_max(self.dedup_window_days)
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise ValueError(retention_range_error(self.dedup_window_days))
            return value
        if key == "timezone":
            return valid_timezone(value)
        if key in BOOL_KEYS:
            if not isinstance(value, bool):
                raise ValueError("Значение должно быть «вкл» или «выкл»")
            return value
        if key == "openrouter_model":
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Название модели не может быть пустым")
            value = value.strip()
            if len(value) > MODEL_MAX_LEN:
                raise ValueError(f"Название модели слишком длинное (максимум {MODEL_MAX_LEN} символов)")
            if not MODEL_RE.match(value):
                raise ValueError("Модель должна быть в формате «провайдер/модель», например openai/gpt-4o-mini")
            return value
        raise ValueError(f"Неизвестная настройка: {key}")

    async def set(self, key: str, value: Any) -> None:
        """Validate, persist, apply in memory and notify subscribers.
        Raises ValueError with a Russian message on invalid input."""
        async with self._lock:
            value = self._validate(key, value)
            await self.repo.set_setting(key, value)
            setattr(self, key, value)
        for cb in list(self._listeners):
            try:
                res = cb(self)
                if inspect.isawaitable(res):
                    await res
            except Exception:
                log.exception("settings listener failed")

    def apply_to(self, options, llm=None) -> None:
        """Copy the current values onto live objects (PipelineOptions, OpenRouterClient)."""
        options.notify_score = self.notify_score
        options.high_fit_score = self.high_fit_score
        options.show_paid_contact = self.show_paid_contact
        options.notifications_paused = self.notifications_paused
        if llm is not None:
            llm.model = self.openrouter_model
