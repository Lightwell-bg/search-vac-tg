"""Channel monitor: instant NewMessage events + periodic polling that also covers
reconnect gaps and channels the account has not joined. Poll is the source of truth
for ``channels.last_message_id``; duplicates between the two paths are dropped by the
(channel, message_id) unique key."""
from __future__ import annotations

import asyncio
import logging

from telethon import events

from .client import ChannelRef, TelegramService

log = logging.getLogger("listener")

MAX_POST_ERRORS = 3  # consecutive "error" outcomes before a poison message is skipped


class ChannelListener:
    def __init__(self, tg: TelegramService, pipeline, repo, channel_configs, *, catchup_limit: int = 50,
                 poll_interval_sec: int = 300, retry_interval_sec: int = 600):
        self.tg = tg
        self.pipeline = pipeline
        self.repo = repo
        self.channel_configs = channel_configs
        self.catchup_limit = catchup_limit
        self.poll_interval = poll_interval_sec
        self.retry_interval = retry_interval_sec
        self.refs: list[ChannelRef] = []
        self._tasks: set[asyncio.Task] = set()
        self._errors: dict[tuple[int, int], int] = {}

    async def setup(self) -> None:
        self.refs = await self.tg.resolve_channels(self.channel_configs)
        if not self.refs:
            raise RuntimeError("no public channels resolved; check config/channels.yaml")
        for ref in self.refs:
            await self.repo.upsert_channel(ref.peer_id, ref.username, ref.title)
            log.info("monitoring @%s (%s)", ref.username, ref.title)
        self.tg.client.add_event_handler(self._on_message,
                                         events.NewMessage(chats=[r.entity for r in self.refs]))

    async def reresolve_missing(self) -> int:
        """Retry channels that failed to resolve at startup (FloodWait, temporary errors)."""
        have = {r.username.lower() for r in self.refs}
        missing = [c for c in self.channel_configs if c.username.lower() not in have]
        if not missing:
            return 0
        added = 0
        for ref in await self.tg.resolve_channels(missing):
            if ref.username.lower() in have:
                continue
            await self.repo.upsert_channel(ref.peer_id, ref.username, ref.title)
            self.tg.client.add_event_handler(self._on_message, events.NewMessage(chats=[ref.entity]))
            self.refs.append(ref)
            have.add(ref.username.lower())
            added += 1
            log.info("monitoring @%s (%s), resolved late", ref.username, ref.title)
        return added

    async def _on_message(self, event) -> None:
        # do not block Telethon's update loop with JEV/LLM/clicks
        task = asyncio.create_task(self.pipeline.process_post(self.tg.to_post(event.message)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def poll_once(self) -> int:
        processed = 0
        for ref in list(self.refs):
            try:
                last = await self.repo.get_last_message_id(ref.peer_id)
                for msg in await self.tg.fetch_new(ref, last, self.catchup_limit):
                    key = (ref.peer_id, msg.id)
                    outcome = await self.pipeline.process_post(self.tg.to_post(msg))
                    if outcome == "error":
                        # not stored durably: keep the cursor so the post is fetched again
                        self._errors[key] = self._errors.get(key, 0) + 1
                        if self._errors[key] < MAX_POST_ERRORS:
                            log.warning("post %s/%s not stored (attempt %s/%s), batch stopped",
                                        ref.username, msg.id, self._errors[key], MAX_POST_ERRORS)
                            break
                        log.error("ERROR skipping poison message @%s/%s after %s failed attempts",
                                  ref.username, msg.id, self._errors[key])
                    self._errors.pop(key, None)
                    # "failed" is stored (retry_pending recovers it), so the cursor moves on
                    await self.repo.set_last_message_id(ref.peer_id, msg.id)
                    processed += 1
            except Exception:
                log.exception("ERROR polling @%s", ref.username)
        return processed

    async def run(self) -> None:
        await self.setup()
        loop = asyncio.get_running_loop()
        next_retry = loop.time() + self.retry_interval
        while True:
            n = await self.poll_once()
            if n:
                log.info("poll: %s new posts", n)
            if loop.time() >= next_retry:
                try:
                    await self.reresolve_missing()
                except Exception:
                    log.exception("ERROR re-resolving channels")
                try:
                    await self.pipeline.retry_pending()
                except Exception:
                    log.exception("ERROR retry_pending")
                next_retry = loop.time() + self.retry_interval
            await asyncio.sleep(self.poll_interval)
