"""NotifyBot: sends job cards to the owner and runs the aiogram dispatcher."""
from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeChat, LinkPreviewOptions

from .cards import build_card
from .handlers import BotHandlers
from .keyboards import job_keyboard

log = logging.getLogger("bot")


class NotifyBot:
    """Implements the pipeline ``Notifier`` protocol."""

    def __init__(self, settings, repo, llm, profile: dict, *, runtime_settings=None, listener=None,
                 pipeline=None) -> None:
        self.settings = settings
        self.repo = repo
        self.runtime_settings = runtime_settings   # RuntimeSettings (src/settings_store.py)
        self.listener = listener                   # ChannelListener registry API
        self.pipeline = pipeline                   # Pipeline (flush_backlog etc.)
        self.bot = Bot(token=settings.notify_bot_token)
        self.handlers = BotHandlers(settings, repo, llm, profile, runtime_settings=runtime_settings,
                                    listener=listener)
        self.dp = Dispatcher(storage=MemoryStorage())
        self.dp.include_router(self.handlers.router)

    def set_profile(self, profile: dict) -> None:
        self.handlers.profile = profile

    async def notify_job(self, job_id: int) -> tuple[int, int | None]:
        """Send the card to the owner. Raises on any failure (the pipeline parks the job for retry)."""
        job = await self.repo.get_job(job_id)
        if job is None:
            raise ValueError(f"job {job_id} not found")
        if self.settings.owner_telegram_id is None:
            raise RuntimeError("OWNER_TELEGRAM_ID is not set")
        message = await self.repo.get_primary_message(job_id)
        sources = await self.repo.get_job_sources(job_id)
        text = build_card(job, message, sources, self.runtime_settings or self.settings, html=True)
        kb = job_keyboard(job_id, message.url if message else None, job.contact_value)
        sent = await self.bot.send_message(
            self.settings.owner_telegram_id, text, parse_mode=ParseMode.HTML, reply_markup=kb,
            link_preview_options=LinkPreviewOptions(is_disabled=True))
        return sent.chat.id, sent.message_id

    async def set_commands(self) -> None:
        """Publish the command list for the owner's chat (fallback: default scope). Never raises."""
        commands = [BotCommand(command="menu", description="Главное меню"),
                    BotCommand(command="channels", description="Каналы"),
                    BotCommand(command="stats", description="Статистика"),
                    BotCommand(command="cancel", description="Отмена ввода")]
        try:
            if self.settings.owner_telegram_id is not None:
                await self.bot.set_my_commands(
                    commands, scope=BotCommandScopeChat(chat_id=self.settings.owner_telegram_id))
                return
        except Exception as e:  # noqa: BLE001
            log.warning("cannot set owner-scoped commands (%s), using default scope", e)
        try:
            await self.bot.set_my_commands(commands)
        except Exception as e:  # noqa: BLE001
            log.warning("cannot set bot commands: %s", e)

    async def start_polling(self) -> None:
        await self.set_commands()
        await self.dp.start_polling(self.bot, handle_signals=False,
                                    allowed_updates=["message", "callback_query"])

    async def close(self) -> None:
        await self.bot.session.close()
