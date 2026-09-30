"""Owner menu: /menu, channels, thresholds, paid contacts, pause, model (aiogram 3, HTML).

Pure builders (texts and keyboards) are separate from the handlers so they can be tested
without aiogram objects. Callback data is compact (< 64 bytes):
``m:main|ch|th|md|st|paid|pause``, ``ch:t|c|d|dy:<tg_id>``, ``ch:add``, ``th:n|h:<delta>``.
All untrusted strings (channel titles/usernames, model names) go through ``html.escape``.
"""
from __future__ import annotations

import html
import logging
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message

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
    "/stats — статистика работы\n"
    "/cancel — отмена ввода\n"
    "Под карточкой заказа: 👍/👎 — обратная связь, ✍️ — черновик отклика "
    "(отправляете заказчику вы сами)."
)
CALLBACK_PREFIX = r"^(m|ch|th):"


class MenuStates(StatesGroup):
    waiting_channel = State()
    waiting_model = State()


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def _btn(text: str, data: str) -> InlineKeyboardButton:
    assert len(data.encode("utf-8")) < 64
    return InlineKeyboardButton(text=text, callback_data=data)


def parse_menu_callback(data: str | None) -> tuple[str, ...] | None:
    """``ch:t:123`` -> ("ch", "t", "123"); ints are validated by the caller via ``to_int``."""
    parts = (data or "").split(":")
    if not 2 <= len(parts) <= 3 or parts[0] not in ("m", "ch", "th"):
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
            f"Каналов: {active} активных")


def main_keyboard(rs) -> InlineKeyboardMarkup:
    paid = "💰 Платные: вкл" if rs.show_paid_contact else "💰 Платные: выкл"
    pause = "▶️ Продолжить" if rs.notifications_paused else "⏸ Пауза"
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("📡 Каналы", "m:ch"), _btn("🎯 Пороги", "m:th")],
        [_btn(paid, "m:paid"), _btn(pause, "m:pause")],
        [_btn("🤖 Модель", "m:md"), _btn("📊 Статистика", "m:st")],
    ])


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


# ------------------------------------------------------------------ handlers


class MenuHandlers:
    def __init__(self, repo, runtime_settings=None, listener=None) -> None:
        self.repo = repo
        self.runtime_settings = runtime_settings
        self.listener = listener

    # -- registration (order matters: commands first, then FSM inputs, then callbacks)

    def register(self, router: Router) -> None:
        m = router.message
        m.register(self.cmd_start, Command("start", "help"))
        m.register(self.cmd_menu, Command("menu"))
        m.register(self.cmd_channels, Command("channels"))
        m.register(self.cmd_stats, Command("stats"))
        m.register(self.cmd_cancel, Command("cancel"))
        m.register(self.on_channel_input, StateFilter(MenuStates.waiting_channel))
        m.register(self.on_model_input, StateFilter(MenuStates.waiting_model))
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

    # -- commands

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
        await message.answer(format_stats(await self.repo.get_stats()), parse_mode=None)

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

    # -- callbacks

    async def on_callback(self, cb: CallbackQuery, state: FSMContext | None = None) -> None:
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
            await self._edit(cb, format_stats(await self.repo.get_stats()), back_keyboard(), html_mode=False)
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
