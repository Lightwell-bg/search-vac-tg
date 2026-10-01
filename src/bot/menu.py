"""Owner menu: /menu, channels, thresholds, paid contacts, pause, model (aiogram 3, HTML).

Pure builders (texts and keyboards) are separate from the handlers so they can be tested
without aiogram objects. Callback data is compact (< 64 bytes):
``m:main|ch|th|md|st|paid|pause``, ``ch:t|c|d|dy:<tg_id>``, ``ch:add``, ``th:n|h:<delta>``.
All untrusted strings (channel titles/usernames, model names) go through ``html.escape``.
"""
from __future__ import annotations

import html
import io
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message

from ..profile.service import ALLOWED_EXTENSIONS, MAX_UPLOAD_BYTES, parse_items
from ..settings_store import POLL_INTERVALS, format_interval, parse_interval
from ..timeutil import fmt_local
from .journal_view import (
    PAGE_SIZE, PERIODS, RETENTION_PROMPT, journal_keyboard, journal_text, page_count, parse_journal_callback,
    retention_keyboard, retention_text,
)
from .stats_format import format_stats

log = logging.getLogger("bot")

MODEL_EXAMPLE = "google/gemini-2.5-flash-lite"
MODELS_URL = "https://openrouter.ai/models"
UNAVAILABLE = "Недоступно: управление настройками не подключено"
NEED_TEXT = "Нужен текст: @username или ссылка"
HELP_TEXT = (
    "Бот присылает подходящие заказы из публичных Telegram-каналов.\n"
    "/menu — меню: каналы, пороги, пауза, модель\n"
    "/channels — список каналов\n"
    "/profile — профиль исполнителя (резюме, навыки)\n"
    "/stats — статистика работы\n"
    "/journal — журнал проверок: что отправлено и почему отсеяно\n"
    "/cancel — отмена ввода\n"
    "Под карточкой заказа: 👍/👎 — обратная связь, ✍️ — черновик отклика "
    "(отправляете заказчику вы сами)."
)
CALLBACK_PREFIX = r"^(m|ch|th|pi|pf|j|jr|tz):"


class MenuStates(StatesGroup):
    waiting_channel = State()
    waiting_model = State()
    waiting_interval = State()
    waiting_upload = State()
    waiting_add_skills = State()
    waiting_remove_skills = State()
    waiting_retention = State()
    waiting_timezone = State()


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def _btn(text: str, data: str) -> InlineKeyboardButton:
    assert len(data.encode("utf-8")) < 64
    return InlineKeyboardButton(text=text, callback_data=data)


def parse_menu_callback(data: str | None) -> tuple[str, ...] | None:
    """``ch:t:123`` -> ("ch", "t", "123"); ints are validated by the caller via ``to_int``."""
    parts = (data or "").split(":")
    if not 2 <= len(parts) <= 3 or parts[0] not in ("m", "ch", "th", "pi", "pf", "jr", "tz"):
        return None
    return tuple(parts)


def to_int(value: str | None) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def channel_label(ch: dict) -> str:
    return f"@{ch['username']}" if ch.get("username") else str(ch.get("title") or ch.get("tg_id"))


# ------------------------------------------------------------------ builders


def main_text(rs, channels: list[dict] | None) -> str:
    state = "⏸ Уведомления на паузе" if rs.notifications_paused else "▶️ Уведомления включены"
    paid = "показывать" if rs.show_paid_contact else "скрывать"
    active = sum(1 for c in (channels or []) if c.get("enabled"))
    return (f"{state}\n"
            f"Порог {rs.notify_score} / высокий {rs.high_fit_score}\n"
            f"Платные контакты: {paid}\n"
            f"Модель: <code>{_e(rs.openrouter_model)}</code>\n"
            f"⏱ Проверка каналов: каждые {format_interval(rs.poll_interval_sec)}\n"
            f"Каналов: {active} активных")


def main_keyboard(rs) -> InlineKeyboardMarkup:
    paid = "💰 Платные: вкл" if rs.show_paid_contact else "💰 Платные: выкл"
    pause = "▶️ Продолжить" if rs.notifications_paused else "⏸ Пауза"
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("📡 Каналы", "m:ch"), _btn("🎯 Пороги", "m:th")],
        [_btn(paid, "m:paid"), _btn(pause, "m:pause")],
        [_btn("🤖 Модель", "m:md"), _btn("📊 Статистика", "m:st")],
        [_btn("⏱ Период проверки", "m:pi"), _btn("👤 Профиль", "m:pr")],
        [_btn("📜 Журнал", "j:all:d:0")],
        [_btn(f"🗑 Хранение журнала: {getattr(rs, 'log_retention_days', 30)} дн", "m:jr")],
        [_btn(f"🕒 Часовой пояс: {getattr(rs, 'timezone', 'Europe/Sofia')}", "m:tz")],
    ])


TIMEZONE_PRESETS = ("Europe/Sofia", "Europe/Moscow", "Europe/Kyiv", "Europe/Berlin", "UTC", "Asia/Almaty")
TIMEZONE_PROMPT = "Пришлите имя часового пояса IANA, например Europe/Warsaw. /cancel — отмена"
TIMEZONE_REMINDER = "Жду текст: имя часового пояса IANA, например Europe/Warsaw. /cancel — отмена"


def timezone_text(rs) -> str:
    return (f"🕒 Часовой пояс: {_e(getattr(rs, 'timezone', 'Europe/Sofia'))}\n"
            f"По нему показывается время в журнале и на экране проверки каналов.")


def timezone_keyboard(rs) -> InlineKeyboardMarkup:
    cur = getattr(rs, "timezone", "Europe/Sofia")
    btns = [_btn(("✅ " if name == cur else "") + name, f"tz:{i}") for i, name in enumerate(TIMEZONE_PRESETS)]
    return InlineKeyboardMarkup(inline_keyboard=[
        btns[:2], btns[2:4], btns[4:],
        [_btn("✏️ Своё (IANA, напр. Europe/Warsaw)", "tz:custom")],
        [_btn("⬅️ Назад", "m:main")]])


INTERVAL_PROMPT = ("Пришлите интервал: число минут (например 500) или с единицей: 90с, 30м, 2ч. "
                   "От 1 мин до 24 ч. /cancel — отмена")
CHECK_TIMEOUT_SEC = 120


def last_poll_line(listener, tz: str = "Europe/Sofia") -> str:
    at = getattr(listener, "last_poll_at", None)
    if not isinstance(at, datetime):
        return "Последняя проверка: ещё не было"
    n = getattr(listener, "last_poll_new", 0)
    return f"Последняя проверка: {fmt_local(at, tz, '%H:%M')} ({n} новых)"


def interval_text(rs, listener=None) -> str:
    text = f"⏱ Как часто проверять каналы\nСейчас: каждые {format_interval(rs.poll_interval_sec)}"
    if listener is not None:
        text += "\n" + last_poll_line(listener, getattr(rs, "timezone", "Europe/Sofia"))
    return text


def interval_keyboard(rs) -> InlineKeyboardMarkup:
    btns = [_btn(("✅ " if v == rs.poll_interval_sec else "") + format_interval(v), f"pi:{v}")
            for v in POLL_INTERVALS]
    return InlineKeyboardMarkup(inline_keyboard=[
        btns[:4], btns[4:],
        [_btn("✏️ Своё значение", "pi:custom")],
        [_btn("🔄 Проверить сейчас", "pi:now")],
        [_btn("⬅️ Назад", "m:main")]])


def channels_text(channels: list[dict]) -> str:
    if not channels:
        return "📡 Каналов нет. Добавьте публичный канал."
    lines = ["📡 Каналы (✅ слушаем, ⏸ выключен; 👆 — нажимать ли кнопку «получить контакт»)"]
    for c in channels:
        mark = "✅" if c.get("enabled") else "⏸"
        title = _e(c.get("title"))
        lines.append(f"{mark} {_e(channel_label(c))}" + (f" — {title}" if title else ""))
    return "\n".join(lines)


def channels_keyboard(channels: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for c in channels:
        tg_id = int(c["tg_id"])
        mark = "✅" if c.get("enabled") else "⏸"
        click = "да" if c.get("click_callbacks") else "нет"
        rows.append([
            _btn(f"{mark} {channel_label(c)}"[:40], f"ch:t:{tg_id}"),
            _btn(f"👆 клик: {click}", f"ch:c:{tg_id}"),
            _btn("🗑", f"ch:d:{tg_id}"),
        ])
    rows.append([_btn("➕ Добавить канал", "ch:add")])
    rows.append([_btn("⬅️ Назад", "m:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_delete_text(ch: dict) -> str:
    return f"Удалить {_e(channel_label(ch))}? Сообщения и заказы останутся, канал перестанут слушать."


def confirm_delete_keyboard(tg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        _btn("✅ Да", f"ch:dy:{int(tg_id)}"), _btn("❌ Нет", "m:ch")]])


def thresholds_text(rs) -> str:
    return (f"🎯 Пороги (0–100)\n"
            f"Уведомлять от: {rs.notify_score}\n"
            f"Высокое соответствие от: {rs.high_fit_score}")


def thresholds_keyboard(rs) -> InlineKeyboardMarkup:
    def row(label: str, key: str, value: int) -> list[InlineKeyboardButton]:
        return [_btn("−5", f"th:{key}:-5"), _btn("−1", f"th:{key}:-1"),
                _btn(f"{label}: {value}", "m:th"),
                _btn("+1", f"th:{key}:+1"), _btn("+5", f"th:{key}:+5")]
    return InlineKeyboardMarkup(inline_keyboard=[
        row("Порог", "n", rs.notify_score),
        row("Высокий", "h", rs.high_fit_score),
        [_btn("⬅️ Назад", "m:main")],
    ])


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Назад", "m:main")]])


def model_prompt(current: str) -> str:
    return (f"🤖 Текущая модель: <code>{_e(current)}</code>\n"
            f"Пришлите новую в формате провайдер/модель, например <code>{MODEL_EXAMPLE}</code>.\n"
            f"Список: {MODELS_URL}\n/cancel — отмена")


CHANNEL_PROMPT = "Пришлите @username или ссылку t.me/… публичного канала. /cancel — отмена"


def forwarded_channel_username(message) -> str | None:
    origin = getattr(message, "forward_origin", None)
    chat = getattr(origin, "chat", None)
    username = getattr(chat, "username", None)
    return f"@{username}" if isinstance(username, str) and username else None


# ------------------------------------------------------------------ profile builders

TELEGRAM_LIMIT = 4096
UPLOAD_PROMPT = ("📎 Пришлите резюме или портфолио файлом: PDF (с текстом), DOCX, MD или TXT, до 10 МБ. "
                 "/cancel — отмена")
PHOTO_REMINDER = "Пришлите файлом (PDF/DOCX/TXT/MD), не фото"
UPLOAD_REMINDER = "Жду файл (PDF/DOCX/TXT/MD). /cancel — отмена"
ADD_SKILLS_PROMPT = "➕ Пришлите навыки через запятую или с новой строки. /cancel — отмена"
REMOVE_SKILLS_PROMPT = "➖ Пришлите навыки, которые убрать, через запятую или с новой строки. /cancel — отмена"


def profile_text(status: dict, compact: str) -> str:
    """Status line + compact profile in <pre>, html-escaped and trimmed to the Telegram limit."""
    head = (f"👤 Профиль исполнителя\n"
            f"Основа: {status.get('base_projects', 0)} проектов из портфолио; "
            f"загружено файлов: {status.get('uploads', 0)}; "
            f"ручные: +{status.get('added', 0)} / −{status.get('removed', 0)}\n"
            f"Так профиль видят JEV и OpenRouter:\n")
    budget = TELEGRAM_LIMIT - len(head) - len("<pre></pre>") - 1
    body = compact
    while True:
        esc = _e(body)
        if len(esc) <= budget or not body:
            break
        body = body[:max(0, len(body) - max(1, len(esc) - budget))].rstrip()
    return f"{head}<pre>{esc}</pre>"


def profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("📎 Загрузить резюме/портфолио", "pf:up")],
        [_btn("➕ Добавить навыки", "pf:add"), _btn("➖ Убрать навыки", "pf:rm")],
        [_btn("📂 Файлы", "pf:fl")],
        [_btn("⬅️ Назад", "m:main")],
    ])


def _size_label(size: int) -> str:
    return f"{size / 1024:.0f} КБ" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} МБ"


def files_text(uploads: list[tuple[str, int]]) -> str:
    if not uploads:
        return "📂 Загруженных файлов нет."
    lines = ["📂 Загруженные файлы (🗑 — удалить):"]
    lines += [f"• {_e(n)} ({_size_label(s)})" for n, s in uploads]
    return "\n".join(lines)


def files_keyboard(uploads: list[tuple[str, str, int]]) -> InlineKeyboardMarkup:
    """``uploads`` is ``[(id, name, size)]``; the callback carries the stable random id."""
    rows = [[_btn(f"🗑 {n}"[:40], f"pf:d:{i}")] for i, n, _ in uploads]
    rows.append([_btn("⬅️ Назад", "m:pr")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_file_keyboard(upload_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        _btn("✅ Да", f"pf:dy:{upload_id}"), _btn("❌ Нет", "pf:fl")]])


def upload_summary(summary: dict) -> str:
    added = summary.get("added") or []
    head = (f"Добавлено в профиль: {', '.join(added)}" if added
            else "Новых навыков не найдено, файл учтён")
    return (f"{head}\nВсего технологий: {summary.get('total', 0)}; проектов: {summary.get('projects', 0)}"
            + ("; профиль пока предварительный" if summary.get("provisional") else ""))


# ------------------------------------------------------------------ handlers


class MenuHandlers:
    def __init__(self, repo, runtime_settings=None, listener=None, profile_service=None) -> None:
        self.profile_service = profile_service
        self.repo = repo
        self.runtime_settings = runtime_settings
        self.listener = listener

    # -- registration (order matters: commands first, then FSM inputs, then callbacks)

    def register(self, router: Router) -> None:
        m = router.message
        m.register(self.cmd_start, Command("start", "help"))
        m.register(self.cmd_menu, Command("menu"))
        m.register(self.cmd_channels, Command("channels"))
        m.register(self.cmd_profile, Command("profile"))
        m.register(self.cmd_stats, Command("stats"))
        m.register(self.cmd_journal, Command("journal"))
        m.register(self.cmd_cancel, Command("cancel"))
        m.register(self.on_channel_input, StateFilter(MenuStates.waiting_channel))
        m.register(self.on_model_input, StateFilter(MenuStates.waiting_model))
        m.register(self.on_interval_input, StateFilter(MenuStates.waiting_interval))
        m.register(self.on_upload_input, StateFilter(MenuStates.waiting_upload))
        m.register(self.on_add_skills_input, StateFilter(MenuStates.waiting_add_skills))
        m.register(self.on_remove_skills_input, StateFilter(MenuStates.waiting_remove_skills))
        m.register(self.on_retention_input, StateFilter(MenuStates.waiting_retention))
        m.register(self.on_timezone_input, StateFilter(MenuStates.waiting_timezone))
        router.callback_query.register(self.on_callback, F.data.regexp(CALLBACK_PREFIX))

    # -- helpers

    @staticmethod
    async def _clear(state: FSMContext | None) -> None:
        if state is not None:
            await state.clear()

    async def _channels(self) -> list[dict]:
        if self.listener is None:
            return []
        try:
            return await self.listener.channels_overview()
        except Exception:  # noqa: BLE001
            log.exception("cannot load channels")
            return []

    @staticmethod
    async def _edit(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup, *, html_mode: bool = True) -> None:
        if cb.message is None:
            return
        try:
            await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML" if html_mode else None,
                                       link_preview_options=LinkPreviewOptions(is_disabled=True))
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                log.debug("cannot edit menu message: %s", e)

    async def _send_main(self, message: Message) -> None:
        rs = self.runtime_settings
        if rs is None:
            await message.answer(UNAVAILABLE, parse_mode=None)
            return
        await message.answer(main_text(rs, await self._channels()), reply_markup=main_keyboard(rs),
                             parse_mode="HTML")

    async def _send_channels(self, message: Message) -> None:
        if self.listener is None:
            await message.answer(UNAVAILABLE, parse_mode=None)
            return
        chs = await self._channels()
        await message.answer(channels_text(chs), reply_markup=channels_keyboard(chs), parse_mode="HTML")

    async def _send_profile(self, message: Message) -> None:
        ps = self.profile_service
        if ps is None:
            await message.answer(UNAVAILABLE, parse_mode=None)
            return
        await message.answer(profile_text(ps.status(), ps.compact()), reply_markup=profile_keyboard(),
                             parse_mode="HTML")

    async def _show_profile(self, cb: CallbackQuery) -> None:
        ps = self.profile_service
        await self._edit(cb, profile_text(ps.status(), ps.compact()), profile_keyboard())

    async def _show_files(self, cb: CallbackQuery) -> None:
        ups = self.profile_service.list_uploads_ids()
        await self._edit(cb, files_text([(n, s) for _, n, s in ups]), files_keyboard(ups))

    # -- commands

    async def cmd_profile(self, message: Message, state: FSMContext | None = None) -> None:
        await self._clear(state)
        await self._send_profile(message)

    async def cmd_start(self, message: Message, state: FSMContext | None = None) -> None:
        await self._clear(state)
        await message.answer(HELP_TEXT, parse_mode=None)

    async def cmd_menu(self, message: Message, state: FSMContext | None = None) -> None:
        await self._clear(state)
        await self._send_main(message)

    async def cmd_channels(self, message: Message, state: FSMContext | None = None) -> None:
        await self._clear(state)
        await self._send_channels(message)

    async def cmd_stats(self, message: Message, state: FSMContext | None = None) -> None:
        await self._clear(state)
        await message.answer(format_stats(await self.repo.get_stats(), self._tz()), parse_mode=None)

    def _tz(self) -> str:
        return getattr(self.runtime_settings, "timezone", "Europe/Sofia")

    async def cmd_journal(self, message: Message, state: FSMContext | None = None) -> None:
        await self._clear(state)
        text, kb = await self._journal_screen("all", "d", 0)
        await message.answer(text, reply_markup=kb, parse_mode="HTML",
                             link_preview_options=LinkPreviewOptions(is_disabled=True))

    async def _journal_screen(self, kind: str, period: str, page: int):
        """Text + keyboard for one journal page (page is clamped to the existing range)."""
        now = datetime.now(timezone.utc)
        c24 = await self.repo.journal_counts(since=now - timedelta(hours=24))
        hours = PERIODS[period][1]
        since = now - timedelta(hours=hours) if hours else None
        counts = c24 if period == "d" else await self.repo.journal_counts(since=since)
        pages = page_count(counts.get(kind, 0))
        page = min(max(0, page), pages - 1)
        entries = await self.repo.journal(since=since, kind=kind, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
        return journal_text(entries, c24, kind, period, page, pages, self._tz()), journal_keyboard(kind, period, page, pages)

    async def _show_journal(self, cb: CallbackQuery, kind: str, period: str, page: int) -> None:
        text, kb = await self._journal_screen(kind, period, page)
        await self._edit(cb, text, kb)

    async def on_retention_input(self, message: Message, state: FSMContext | None = None) -> None:
        rs = self.runtime_settings
        if rs is None:
            await self._clear(state)
            await message.answer(UNAVAILABLE, parse_mode=None)
            return
        text = (message.text or "").strip()
        if not text:
            await message.answer(RETENTION_PROMPT, parse_mode=None)
            return
        try:
            await rs.set("log_retention_days", int(text))
        except ValueError as e:
            await message.answer(f"{e}\nПопробуйте ещё раз или /cancel", parse_mode=None)
            return
        await self._clear(state)
        await message.answer(f"✅ Хранение журнала: {rs.log_retention_days} дн", parse_mode=None)
        await message.answer(retention_text(rs), reply_markup=retention_keyboard(rs), parse_mode="HTML")

    async def on_timezone_input(self, message: Message, state: FSMContext | None = None) -> None:
        rs = self.runtime_settings
        if rs is None:
            await self._clear(state)
            await message.answer(UNAVAILABLE, parse_mode=None)
            return
        text = (message.text or "").strip()
        if not text:
            await message.answer(TIMEZONE_REMINDER, parse_mode=None)
            return
        try:
            await rs.set("timezone", text)
        except ValueError as e:
            await message.answer(f"{e}\nПопробуйте ещё раз или /cancel", parse_mode=None)
            return
        await self._clear(state)
        await message.answer(f"✅ Часовой пояс: {rs.timezone}", parse_mode=None)
        await message.answer(timezone_text(rs), reply_markup=timezone_keyboard(rs), parse_mode="HTML")

    async def cmd_cancel(self, message: Message, state: FSMContext | None = None) -> None:
        await self._clear(state)
        await message.answer("Отменено. /menu — главное меню", parse_mode=None)

    # -- FSM inputs

    async def on_channel_input(self, message: Message, state: FSMContext | None = None) -> None:
        if self.listener is None:
            await self._clear(state)
            await message.answer(UNAVAILABLE, parse_mode=None)
            return
        text = forwarded_channel_username(message) or (message.text or "").strip()
        if not text:
            await message.answer(NEED_TEXT, parse_mode=None)
            return
        ok, msg = await self.listener.add_channel(text)
        if not ok:
            await message.answer(f"{msg}\nПопробуйте ещё раз или /cancel", parse_mode=None)
            return
        await self._clear(state)
        await message.answer(msg, parse_mode=None)
        await self._send_channels(message)

    async def on_model_input(self, message: Message, state: FSMContext | None = None) -> None:
        rs = self.runtime_settings
        if rs is None:
            await self._clear(state)
            await message.answer(UNAVAILABLE, parse_mode=None)
            return
        text = (message.text or "").strip()
        if not text:
            await message.answer(NEED_TEXT.replace("@username или ссылка", "название модели"), parse_mode=None)
            return
        try:
            await rs.set("openrouter_model", text)
        except ValueError as e:
            await message.answer(f"{e}\nПопробуйте ещё раз или /cancel", parse_mode=None)
            return
        await self._clear(state)
        await message.answer(f"Модель изменена: {rs.openrouter_model}", parse_mode=None)
        await self._send_main(message)

    async def on_interval_input(self, message: Message, state: FSMContext | None = None) -> None:
        rs = self.runtime_settings
        if rs is None:
            await self._clear(state)
            await message.answer(UNAVAILABLE, parse_mode=None)
            return
        try:
            sec = parse_interval(message.text or "")
            await rs.set("poll_interval_sec", sec)
        except ValueError as e:
            await message.answer(f"{e}\nПопробуйте ещё раз или /cancel", parse_mode=None)
            return
        await self._clear(state)
        await message.answer(f"✅ Проверка каналов: каждые {format_interval(rs.poll_interval_sec)}", parse_mode=None)
        await message.answer(interval_text(rs, self.listener), reply_markup=interval_keyboard(rs),
                             parse_mode="HTML")

    async def on_upload_input(self, message: Message, state: FSMContext | None = None) -> None:
        ps = self.profile_service
        if ps is None:
            await self._clear(state)
            await message.answer(UNAVAILABLE, parse_mode=None)
            return
        doc = getattr(message, "document", None)
        if doc is None:
            await message.answer(PHOTO_REMINDER if getattr(message, "photo", None) else UPLOAD_REMINDER,
                                 parse_mode=None)
            return
        limit_mb = MAX_UPLOAD_BYTES // (1024 * 1024)
        if Path(doc.file_name or "").suffix.lower() not in ALLOWED_EXTENSIONS:
            await message.answer("Допустимые форматы: PDF, DOCX, MD, TXT. Пришлите другой файл или /cancel",
                                 parse_mode=None)
            return
        size = doc.file_size
        if not isinstance(size, int) or size <= 0 or size > MAX_UPLOAD_BYTES:
            await message.answer(f"Размер файла неизвестен или больше {limit_mb} МБ. Пришлите другой или /cancel",
                                 parse_mode=None)
            return
        try:
            dest = io.BytesIO()
            buf = await message.bot.download(doc, destination=dest)
            buf = buf if buf is not None else dest
            buf.seek(0)
            content = buf.read(MAX_UPLOAD_BYTES + 1)
            if len(content) > MAX_UPLOAD_BYTES:
                await message.answer(f"Файл больше {limit_mb} МБ. Пришлите другой или /cancel", parse_mode=None)
                return
            summary = await ps.add_upload(doc.file_name or "file", content)
        except ValueError as e:
            await message.answer(f"{e}\nПопробуйте ещё раз или /cancel", parse_mode=None)
            return
        except Exception:  # noqa: BLE001 - keep the state, let the owner retry
            log.exception("profile upload failed")
            await message.answer("Не удалось обработать файл. Попробуйте ещё раз или /cancel", parse_mode=None)
            return
        await self._clear(state)
        await message.answer(upload_summary(summary), parse_mode=None)
        await self._send_profile(message)

    async def _skills_input(self, message: Message, state, add: bool) -> None:
        ps = self.profile_service
        if ps is None:
            await self._clear(state)
            await message.answer(UNAVAILABLE, parse_mode=None)
            return
        items = parse_items(message.text or "")
        if not items:
            await message.answer("Нужен текст: навыки через запятую или с новой строки", parse_mode=None)
            return
        try:
            changed = await (ps.add_skills(items) if add else ps.remove_skills(items))
        except ValueError as e:
            await message.answer(f"{e}\nПопробуйте ещё раз или /cancel", parse_mode=None)
            return
        await self._clear(state)
        word = "Добавлено" if add else "Убрано"
        await message.answer(f"{word}: {', '.join(changed)}" if changed else "Без изменений", parse_mode=None)
        await self._send_profile(message)

    async def on_add_skills_input(self, message: Message, state: FSMContext | None = None) -> None:
        await self._skills_input(message, state, True)

    async def on_remove_skills_input(self, message: Message, state: FSMContext | None = None) -> None:
        await self._skills_input(message, state, False)

    # -- callbacks

    async def on_callback(self, cb: CallbackQuery, state: FSMContext | None = None) -> None:
        if (cb.data or "").startswith("j:"):
            await self._on_journal(cb, state)
            return
        parts = parse_menu_callback(cb.data)
        if parts is None:
            await cb.answer()
            return
        await self._clear(state)
        rs = self.runtime_settings
        kind, action = parts[0], parts[1]
        arg = parts[2] if len(parts) == 3 else None
        try:
            if kind == "m":
                await self._on_menu(cb, state, action, rs)
            elif kind == "ch":
                await self._on_channel(cb, state, action, arg)
            elif kind == "pf":
                await self._on_profile(cb, state, action, arg)
            elif kind == "jr":
                await self._on_retention(cb, state, action, rs)
            elif kind == "tz":
                await self._on_timezone(cb, state, action, rs)
            elif kind == "pi":
                await self._on_interval(cb, state, action, rs)
            else:
                await self._on_threshold(cb, action, arg, rs)
        except Exception:  # noqa: BLE001 - a menu press must never crash the dispatcher
            log.exception("menu callback failed: %s", cb.data)
            await cb.answer("Ошибка, попробуйте ещё раз", show_alert=True)

    async def _show_main(self, cb: CallbackQuery, rs) -> None:
        await self._edit(cb, main_text(rs, await self._channels()), main_keyboard(rs))

    async def _show_channels(self, cb: CallbackQuery) -> None:
        chs = await self._channels()
        await self._edit(cb, channels_text(chs), channels_keyboard(chs))

    async def _on_menu(self, cb: CallbackQuery, state, action: str, rs) -> None:
        if action == "st":
            await self._edit(cb, format_stats(await self.repo.get_stats(), self._tz()), back_keyboard(),
                             html_mode=False)
            await cb.answer()
            return
        if action == "ch":
            if self.listener is None:
                await cb.answer(UNAVAILABLE, show_alert=True)
                return
            await self._show_channels(cb)
            await cb.answer()
            return
        if rs is None:
            await cb.answer(UNAVAILABLE, show_alert=True)
            return
        if action == "main":
            await self._show_main(cb, rs)
        elif action == "th":
            await self._edit(cb, thresholds_text(rs), thresholds_keyboard(rs))
        elif action == "pr":
            if self.profile_service is None:
                await cb.answer(UNAVAILABLE, show_alert=True)
                return
            await self._show_profile(cb)
        elif action == "pi":
            await self._edit(cb, interval_text(rs, self.listener), interval_keyboard(rs))
        elif action == "jr":
            await self._edit(cb, retention_text(rs), retention_keyboard(rs))
        elif action == "tz":
            await self._edit(cb, timezone_text(rs), timezone_keyboard(rs))
        elif action == "md":
            if state is not None:
                await state.set_state(MenuStates.waiting_model)
            await self._edit(cb, model_prompt(rs.openrouter_model), back_keyboard())
        elif action in ("paid", "pause"):
            key, value = (("show_paid_contact", not rs.show_paid_contact) if action == "paid"
                          else ("notifications_paused", not rs.notifications_paused))
            try:
                await rs.set(key, value)
            except ValueError as e:
                await cb.answer(str(e)[:200], show_alert=True)
                return
            await self._show_main(cb, rs)
        await cb.answer()

    async def _on_journal(self, cb: CallbackQuery, state) -> None:
        await self._clear(state)
        parsed = parse_journal_callback(cb.data) or ("all", "d", 0)
        try:
            await self._show_journal(cb, *parsed)
            await cb.answer()
        except Exception:  # noqa: BLE001
            log.exception("journal callback failed: %s", cb.data)
            await cb.answer("Ошибка, попробуйте ещё раз", show_alert=True)

    async def _on_retention(self, cb: CallbackQuery, state, action: str, rs) -> None:
        if rs is None:
            await cb.answer(UNAVAILABLE, show_alert=True)
            return
        if action == "custom":
            if state is not None:
                await state.set_state(MenuStates.waiting_retention)
            await self._edit(cb, RETENTION_PROMPT, InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Назад", "m:jr")]]),
                             html_mode=False)
            await cb.answer()
            return
        days = to_int(action)
        if days is None:
            await cb.answer()
            return
        try:
            await rs.set("log_retention_days", days)
        except ValueError as e:
            await cb.answer(str(e)[:200], show_alert=True)
            return
        await self._edit(cb, retention_text(rs), retention_keyboard(rs))
        await cb.answer()

    async def _on_timezone(self, cb: CallbackQuery, state, action: str, rs) -> None:
        if rs is None:
            await cb.answer(UNAVAILABLE, show_alert=True)
            return
        if action == "custom":
            if state is not None:
                await state.set_state(MenuStates.waiting_timezone)
            await self._edit(cb, TIMEZONE_PROMPT, InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Назад", "m:tz")]]),
                             html_mode=False)
            await cb.answer()
            return
        idx = to_int(action)
        if idx is None or not 0 <= idx < len(TIMEZONE_PRESETS):
            await cb.answer()
            return
        try:
            await rs.set("timezone", TIMEZONE_PRESETS[idx])
        except ValueError as e:
            await cb.answer(str(e)[:200], show_alert=True)
            return
        await self._edit(cb, timezone_text(rs), timezone_keyboard(rs))
        await cb.answer()

    async def _on_profile(self, cb: CallbackQuery, state, action: str, arg: str | None) -> None:
        ps = self.profile_service
        if ps is None:
            await cb.answer(UNAVAILABLE, show_alert=True)
            return
        prompts = {"up": (MenuStates.waiting_upload, UPLOAD_PROMPT),
                   "add": (MenuStates.waiting_add_skills, ADD_SKILLS_PROMPT),
                   "rm": (MenuStates.waiting_remove_skills, REMOVE_SKILLS_PROMPT)}
        if action in prompts:
            new_state, prompt = prompts[action]
            if state is not None:
                await state.set_state(new_state)
            await self._edit(cb, prompt, InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Назад", "m:pr")]]),
                             html_mode=False)
        elif action == "fl":
            await self._show_files(cb)
        elif action in ("d", "dy"):
            name = ps.name_for_id(arg or "")
            if name is None:
                await cb.answer("Список файлов изменился", show_alert=True)
                await self._show_files(cb)
                return
            if action == "d":
                await self._edit(cb, f"Удалить файл {_e(name)}? Навыки из него пропадут из профиля.",
                                 confirm_file_keyboard(arg))
            else:
                await ps.delete_upload(name)
                await self._show_files(cb)
        await cb.answer()

    async def _on_channel(self, cb: CallbackQuery, state, action: str, arg: str | None) -> None:
        lst = self.listener
        if lst is None:
            await cb.answer(UNAVAILABLE, show_alert=True)
            return
        if action == "add":
            if state is not None:
                await state.set_state(MenuStates.waiting_channel)
            await self._edit(cb, CHANNEL_PROMPT, back_keyboard(), html_mode=False)
            await cb.answer()
            return
        tg_id = to_int(arg)
        if tg_id is None:
            await cb.answer()
            return
        chs = await self._channels()
        ch = next((c for c in chs if int(c["tg_id"]) == tg_id), None)
        if ch is None:
            await cb.answer("Канал не найден", show_alert=True)
            await self._show_channels(cb)
            return
        if action == "t":
            await lst.set_enabled(tg_id, not ch["enabled"])
        elif action == "c":
            await lst.set_click(tg_id, not ch["click_callbacks"])
        elif action == "d":
            await self._edit(cb, confirm_delete_text(ch), confirm_delete_keyboard(tg_id))
            await cb.answer()
            return
        elif action == "dy":
            await lst.remove_channel(tg_id)
        else:
            await cb.answer()
            return
        await self._show_channels(cb)
        await cb.answer()

    async def _check_now(self, cb: CallbackQuery, rs) -> None:
        lst = self.listener
        if lst is None:
            await cb.answer(UNAVAILABLE, show_alert=True)
            return
        done = lst.request_poll_now()
        if done is None:
            await cb.answer("Проверка уже идёт", show_alert=True)
            return
        await cb.answer("Проверяю каналы…")
        try:
            await asyncio.wait_for(done.wait(), timeout=CHECK_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            if cb.message is not None:
                await cb.message.answer("Проверка ещё идёт, результат появится позже", parse_mode=None)
            return
        if cb.message is not None:
            await cb.message.answer(f"Проверено: {lst.last_poll_new} новых постов", parse_mode=None)
        await self._edit(cb, interval_text(rs, lst), interval_keyboard(rs))

    async def _on_interval(self, cb: CallbackQuery, state, action: str, rs) -> None:
        if rs is None:
            await cb.answer(UNAVAILABLE, show_alert=True)
            return
        if action == "custom":
            if state is not None:
                await state.set_state(MenuStates.waiting_interval)
            await self._edit(cb, INTERVAL_PROMPT, InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Назад", "m:pi")]]),
                             html_mode=False)
            await cb.answer()
            return
        if action == "now":
            await self._check_now(cb, rs)
            return
        value = to_int(action)
        if value not in POLL_INTERVALS:
            await cb.answer()
            return
        try:
            await rs.set("poll_interval_sec", value)
        except ValueError as e:
            await cb.answer(str(e)[:200], show_alert=True)
            return
        await self._edit(cb, interval_text(rs, self.listener), interval_keyboard(rs))
        await cb.answer()

    async def _on_threshold(self, cb: CallbackQuery, key: str, arg: str | None, rs) -> None:
        name = {"n": "notify_score", "h": "high_fit_score"}.get(key)
        delta = to_int(arg)
        if rs is None:
            await cb.answer(UNAVAILABLE, show_alert=True)
            return
        if name is None or delta is None or abs(delta) > 10:
            await cb.answer()
            return
        try:
            await rs.set(name, getattr(rs, name) + delta)
        except ValueError as e:
            await cb.answer(str(e)[:200], show_alert=True)
            return
        await self._edit(cb, thresholds_text(rs), thresholds_keyboard(rs))
        await cb.answer()
