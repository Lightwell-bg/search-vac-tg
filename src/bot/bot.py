"""NotifyBot: sends job cards to the owner and runs the aiogram dispatcher."""
from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.types import LinkPreviewOptions

from .cards import build_card
from .handlers import BotHandlers
from .keyboards import job_keyboard

log = logging.getLogger("bot")


class NotifyBot:
    """Implements the pipeline ``Notifier`` protocol."""

    def __init__(self, settings, repo, llm, profile: dict) -> None:
        self.settings = settings
        self.repo = repo
        self.bot = Bot(token=settings.notify_bot_token)
        self.handlers = BotHandlers(settings, repo, llm, profile)
        self.dp = Dispatcher()
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
        text = build_card(job, message, sources, self.settings, html=True)
        kb = job_keyboard(job_id, message.url if message else None, job.contact_value)
        sent = await self.bot.send_message(
            self.settings.owner_telegram_id, text, parse_mode=ParseMode.HTML, reply_markup=kb,
            link_preview_options=LinkPreviewOptions(is_disabled=True))
        return sent.chat.id, sent.message_id

    async def start_polling(self) -> None:
        await self.dp.start_polling(self.bot, handle_signals=False,
                                    allowed_updates=["message", "callback_query"])

    async def close(self) -> None:
        await self.bot.session.close()
