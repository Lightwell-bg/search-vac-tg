"""aiogram handlers. Everything from anyone except OWNER_TELEGRAM_ID is ignored."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message, TelegramObject

from ..llm.schemas import LlmError
from ..profile.loader import compact_profile
from ..profile.matcher import relevant_projects
from .keyboards import job_keyboard
from .stats_format import format_stats

log = logging.getLogger("bot")

HELP_TEXT = (
    "Бот присылает подходящие заказы из публичных Telegram-каналов.\n"
    "/stats — статистика работы\n"
    "Под карточкой заказа: 👍/👎 — обратная связь, ✍️ — черновик отклика "
    "(отправляете заказчику вы сами)."
)
APPLICATION_NOTE = "Отправьте заказчику сами — автоматически ничего не отправляется."


def is_owner(owner_id: int | None, user_id: int | None) -> bool:
    """Pure owner check; with no owner configured nobody is allowed."""
    return owner_id is not None and user_id is not None and int(user_id) == int(owner_id)


def parse_callback(data: str | None) -> tuple[str, str | None, int] | None:
    """``c:5`` -> ("c", None, 5); ``f:up:5`` -> ("f", "up", 5); None if malformed."""
    parts = (data or "").split(":")
    try:
        if len(parts) == 2 and parts[0] in ("c", "a"):
            return parts[0], None, int(parts[1])
        if len(parts) == 3 and parts[0] == "f" and parts[1] in ("up", "down"):
            return "f", parts[1], int(parts[2])
    except ValueError:
        return None
    return None


class OwnerOnlyMiddleware(BaseMiddleware):
    """Drops updates from anyone but the owner; callbacks are acknowledged silently."""

    def __init__(self, owner_id: int | None) -> None:
        self.owner_id = owner_id

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        user = getattr(event, "from_user", None)
        if is_owner(self.owner_id, getattr(user, "id", None)):
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            try:
                await event.answer()
            except Exception:  # noqa: BLE001 - best effort
                pass
        log.debug("ignored update from non-owner")
        return None


class BotHandlers:
    def __init__(self, settings, repo, llm, profile: dict) -> None:
        self.settings = settings
        self.repo = repo
        self.llm = llm
        self.profile = profile
        self._generating: set[int] = set()
        self._tasks: set[asyncio.Task] = set()
        self.router = Router(name="owner")
        mw = OwnerOnlyMiddleware(settings.owner_telegram_id)
        self.router.message.outer_middleware(mw)
        self.router.callback_query.outer_middleware(mw)
        self.router.message.register(self.cmd_start, Command("start", "help"))
        self.router.message.register(self.cmd_stats, Command("stats"))
        self.router.callback_query.register(self.on_callback)

    # ---------------------------------------------------------------- commands

    async def cmd_start(self, message: Message) -> None:
        await message.answer(HELP_TEXT, parse_mode=None)

    async def cmd_stats(self, message: Message) -> None:
        stats = await self.repo.get_stats()
        await message.answer(format_stats(stats), parse_mode=None)

    # --------------------------------------------------------------- callbacks

    async def on_callback(self, cb: CallbackQuery) -> None:
        parsed = parse_callback(cb.data)
        if parsed is None:
            await cb.answer()
            return
        kind, arg, job_id = parsed
        job = await self.repo.get_job(job_id)
        if job is None:
            await cb.answer("Заказ не найден", show_alert=True)
            return
        if kind == "c":
            await cb.answer((job.contact_value or "контакт не найден")[:200], show_alert=True)
        elif kind == "f":
            await self._feedback(cb, job, arg or "up")
        else:
            await self._start_application(cb, job)

    async def _feedback(self, cb: CallbackQuery, job, value: str) -> None:
        snapshot = {
            "rules": job.rules_result, "jev": job.jev_result, "llm": job.llm_result,
            "final": {"fit_score": job.fit_score, "route": job.route,
                      "decision_reason": job.decision_reason, "status": job.status},
        }
        await self.repo.save_feedback(job.id, value, snapshot)
        await cb.answer("Сохранено 👍" if value == "up" else "Сохранено 👎")
        msg = await self.repo.get_primary_message(job.id)
        kb = job_keyboard(job.id, msg.url if msg else None, job.contact_value, chosen=value)
        if cb.message is not None:
            try:
                await cb.message.edit_reply_markup(reply_markup=kb)
            except Exception as e:  # noqa: BLE001 - e.g. "message is not modified"
                log.debug("cannot edit keyboard: %s", e)

    async def _start_application(self, cb: CallbackQuery, job) -> None:
        if job.id in self._generating:
            await cb.answer("Уже генерирую…")
            return
        self._generating.add(job.id)
        await cb.answer("Генерирую…")
        chat_id = cb.message.chat.id if cb.message is not None else self.settings.owner_telegram_id
        reply_to = cb.message.message_id if cb.message is not None else None
        task = asyncio.create_task(self._generate(cb.bot, chat_id, reply_to, job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _generate(self, bot, chat_id: int, reply_to: int | None, job) -> None:
        try:
            if self.llm is None:
                await self._send(bot, chat_id, reply_to,
                                 "Не удалось сгенерировать отклик: OpenRouter не настроен")
                return
            try:
                text, usage = await self.llm.write_application(
                    compact_profile(self.profile), job.normalized_text,
                    relevant_projects(self.profile, job.normalized_text))
            except LlmError as e:
                u = e.usage
                await self._log_usage(job.id, u, error=str(e)[:500])
                log.warning("application generation failed for job %s: %s", job.id, e)
                await self._send(bot, chat_id, reply_to, f"Не удалось сгенерировать отклик: {e.kind}")
                return
            await self._log_usage(job.id, usage)
            text = (text or "").strip()
            if not text:
                await self._send(bot, chat_id, reply_to, "Не удалось сгенерировать отклик: empty")
                return
            body = text[:3600] + "\n\n" + APPLICATION_NOTE
            sent = await self._send(bot, chat_id, reply_to, body)
            # the draft is delivered: bookkeeping failures must not produce a "failed" message
            try:
                await self.repo.save_notification(job.id, chat_id, getattr(sent, "message_id", None),
                                                  kind="application")
            except Exception:  # noqa: BLE001
                log.exception("cannot save application notification for job %s", job.id)
        except Exception:  # noqa: BLE001 - background task must never die silently
            log.exception("application task failed for job %s", job.id)
            try:
                await self._send(bot, chat_id, reply_to, "Не удалось сгенерировать отклик: internal")
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._generating.discard(job.id)

    async def _log_usage(self, job_id: int, usage, error: str | None = None) -> None:
        try:
            await self.repo.log_llm_usage(
                job_id=job_id, purpose="application_generation",
                model=(usage.model if usage else getattr(self.llm, "model", "")) or "",
                input_tokens=usage.input_tokens if usage else 0,
                output_tokens=usage.output_tokens if usage else 0,
                cost_usd=usage.cost_usd if usage else 0.0,
                duration_ms=usage.duration_ms if usage else 0, error=error)
        except Exception:  # noqa: BLE001
            log.exception("cannot log llm usage for job %s", job_id)

    @staticmethod
    async def _send(bot, chat_id: int, reply_to: int | None, text: str):
        return await bot.send_message(
            chat_id, text, parse_mode=None, reply_to_message_id=reply_to,
            allow_sending_without_reply=True,
            link_preview_options=LinkPreviewOptions(is_disabled=True))


__all__ = ["BotHandlers", "OwnerOnlyMiddleware", "is_owner", "parse_callback"]
