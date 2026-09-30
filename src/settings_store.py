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

log = logging.getLogger("settings")

MODEL_RE = re.compile(r"^[\w.\-~:]+/[\w.\-~:]+$")
MODEL_MAX_LEN = 100

INT_KEYS = ("notify_score", "high_fit_score")
BOOL_KEYS = ("show_paid_contact", "notifications_paused")
ALL_KEYS = INT_KEYS + BOOL_KEYS + ("openrouter_model", "poll_interval_sec")
POLL_INTERVALS = (60, 120, 300, 600, 900, 1800, 3600)


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
                 notifications_paused: bool, openrouter_model: str, poll_interval_sec: int = 300) -> None:
        self.repo = repo
        self.notify_score = notify_score
        self.high_fit_score = high_fit_score
        self.show_paid_contact = show_paid_contact
        self.notifications_paused = notifications_paused
        self.openrouter_model = openrouter_model
        self.poll_interval_sec = poll_interval_sec
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
        }
        stored = await repo.get_settings()
        for key in ALL_KEYS:
            if key not in stored:
                continue
            v = stored[key]
            ok = (isinstance(v, bool) if key in BOOL_KEYS
                  else isinstance(v, str) if key == "openrouter_model"
                  else isinstance(v, int) and not isinstance(v, bool))
            if ok and key == "poll_interval_sec" and v not in POLL_INTERVALS:
                ok = False
            if ok:
                values[key] = v
            else:
                log.warning("ignoring invalid stored setting %s=%r", key, v)
        return cls(repo, **values)

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
            if isinstance(value, bool) or not isinstance(value, int) or value not in POLL_INTERVALS:
                raise ValueError("Период проверки должен быть одним из: "
                                 + ", ".join(f"{v // 60} мин" for v in POLL_INTERVALS))
            return value
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
