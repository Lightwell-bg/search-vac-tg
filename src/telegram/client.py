"""Telethon wrapper: the user account session, public channel reading and the two
Telegram actions the contact resolver is allowed to perform.

Public channels are read with ``iter_messages`` (works without joining) plus a
NewMessage handler (instant, for channels the account has joined manually).
The service never joins channels, never calls payments.* methods and never sends
the 2FA password to a bot.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from telethon import TelegramClient, errors, utils
from telethon.tl import types as tl
from telethon.tl.functions.messages import GetBotCallbackAnswerRequest

from ..config import ChannelConfig
from .contact_resolver import CallbackAnswer
from .parser import ButtonInfo, RawPost, buttons_from_message, message_text_with_links

log = logging.getLogger("telegram")


def secure_session_files(session_path: str) -> None:
    """Best effort: session file 0600 and its directory 0700 (no-op/ignored on Windows)."""
    path = Path(session_path + ".session")
    for target, mode in ((path, 0o600), (path.parent, 0o700)):
        try:
            os.chmod(target, mode)
        except OSError:
            pass


@dataclass
class ChannelRef:
    username: str
    peer_id: int          # -100... id, same as message.chat_id
    entity: object
    title: str | None


class TelegramService:
    def __init__(self, session_file, api_id: int, api_hash: str, flood_sleep_threshold: int = 60,
                 click_delay_sec: float = 2.0):
        self.session_path = str(session_file)
        self.client = TelegramClient(str(session_file), api_id, api_hash,
                                     flood_sleep_threshold=flood_sleep_threshold,
                                     auto_reconnect=True, connection_retries=-1, retry_delay=5)
        self.click_delay = click_delay_sec
        self.channels: dict[int, ChannelRef] = {}
        self._click_lock = asyncio.Lock()

    async def start(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorized: run `python scripts/telegram_login.py`")
        secure_session_files(self.session_path)
        me = await self.client.get_me()
        log.info("Telegram user session ready (id=%s)", me.id)

    async def stop(self) -> None:
        await self.client.disconnect()

    async def resolve_channels(self, configs: list[ChannelConfig]) -> list[ChannelRef]:
        """Resolve public channel usernames. Private/unknown ones are skipped with a warning."""
        refs = []
        for cfg in configs:
            entity = None
            for attempt in range(1, 4):  # FloodWait: sleep and retry the SAME channel
                try:
                    entity = await self.client.get_entity(cfg.username)
                    break
                except (ValueError, errors.UsernameInvalidError, errors.UsernameNotOccupiedError) as e:
                    log.error("channel @%s not found: %s", cfg.username, e)
                    break
                except errors.FloodWaitError as e:
                    log.warning("FloodWait %ss resolving @%s (attempt %s/3)", e.seconds, cfg.username, attempt)
                    if attempt < 3:
                        await asyncio.sleep(e.seconds + 1)
                    else:
                        log.error("channel @%s skipped after repeated FloodWait", cfg.username)
            if entity is None:
                continue
            if not isinstance(entity, tl.Channel) or not getattr(entity, "username", None):
                log.error("@%s is not a public channel, skipped", cfg.username)
                continue
            ref = ChannelRef(entity.username, utils.get_peer_id(entity), entity, entity.title)
            self.channels[ref.peer_id] = ref
            refs.append(ref)
        return refs

    def to_post(self, message) -> RawPost:
        ref = self.channels.get(message.chat_id)
        return RawPost(
            channel_tg_id=message.chat_id,
            channel_username=ref.username if ref else None,
            message_id=message.id,
            date=message.date,
            text=message_text_with_links(message),
            buttons=buttons_from_message(message),
        )

    async def fetch_new(self, ref: ChannelRef, after_id: int, first_run_limit: int, max_batch: int = 200):
        """Messages newer than ``after_id``, oldest first. On the first run (after_id=0)
        only the latest ``first_run_limit`` posts are taken."""
        try:
            if after_id <= 0:
                msgs = [m async for m in self.client.iter_messages(ref.entity, limit=first_run_limit)]
                msgs.reverse()
            else:
                msgs = [m async for m in self.client.iter_messages(
                    ref.entity, min_id=after_id, reverse=True, limit=max_batch)]
        except errors.FloodWaitError as e:
            log.warning("FloodWait %ss reading @%s", e.seconds, ref.username)
            await asyncio.sleep(e.seconds + 1)
            return []
        return [m for m in msgs if isinstance(m, tl.Message)]

    # ---------------------------------------------------- TelegramActions (resolver)

    async def click_callback(self, post: RawPost, button: ButtonInfo) -> CallbackAnswer:
        """Standard press of a public callback button: no password, no payment form."""
        if button.kind != "callback" or button.requires_password or button.data is None:
            raise ValueError("only plain callback buttons may be pressed")
        ref = self.channels.get(post.channel_tg_id)
        peer = ref.entity if ref else post.channel_tg_id
        async with self._click_lock:  # one click at a time, spaced out
            await asyncio.sleep(self.click_delay)
            try:
                ans = await self.client(GetBotCallbackAnswerRequest(peer=peer, msg_id=post.message_id,
                                                                    data=button.data))
            except errors.FloodWaitError as e:
                log.warning("FloodWait %ss on callback", e.seconds)
                raise
        return CallbackAnswer(message=getattr(ans, "message", None), url=getattr(ans, "url", None),
                              alert=bool(getattr(ans, "alert", False)))

    async def refetch(self, post: RawPost) -> tuple[str, list[ButtonInfo]] | None:
        ref = self.channels.get(post.channel_tg_id)
        peer = ref.entity if ref else post.channel_tg_id
        msg = await self.client.get_messages(peer, ids=post.message_id)
        if not msg:
            return None
        return message_text_with_links(msg), buttons_from_message(msg)
