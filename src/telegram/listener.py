"""Channel monitor: instant NewMessage events + periodic polling that also covers
reconnect gaps and channels the account has not joined. Poll is the source of truth
for ``channels.last_message_id``; duplicates between the two paths are dropped by the
(channel, message_id) unique key.

The DB ``channels`` table is the source of truth for WHICH channels are watched
(``enabled``) and whether contact buttons may be pressed (``click_callbacks``);
config/channels.yaml only seeds it. One NewMessage handler without a ``chats`` filter
drops events from channels outside the enabled set, so adding/removing channels at
runtime never touches handlers. The bot uses the public API: ``add_channel``,
``set_enabled``, ``set_click``, ``remove_channel``, ``channels_overview``."""
from __future__ import annotations

import asyncio
import logging
import re

from telethon import errors, events

from ..config import ChannelConfig
from .client import ChannelLookupError, ChannelRef, TelegramService

log = logging.getLogger("listener")

MAX_POST_ERRORS = 3  # consecutive "error" outcomes before a poison message is skipped
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
DELETED_KEY = "deleted_channels"          # legacy/auxiliary: usernames
DELETED_IDS_KEY = "deleted_channel_ids"   # tombstones keyed by the stable peer id
SEEDED_KEY = "channels_seeded"
_URL_PREFIX = re.compile(r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.me/", re.I)


def parse_channel_username(text: str) -> tuple[str | None, str | None]:
    """Extract a public username from '@name', 'name', 'https://t.me/name', 't.me/name'.
    Returns (username, None) or (None, Russian error)."""
    raw = (text or "").strip()
    if not raw:
        return None, "Укажите username канала, например @freelance_jobs"
    m = _URL_PREFIX.match(raw)
    if m:
        rest = raw[m.end():]
        first = rest.split("/", 1)[0].split("?", 1)[0].strip()
        if first.startswith("+") or first.lower() in ("joinchat", "c", "addlist", "s"):
            return None, "Ссылки-приглашения и приватные каналы не поддерживаются: нужен публичный @username"
        name = first
    else:
        if "/" in raw or "?" in raw:
            return None, "Не удалось разобрать ссылку: используйте @username или https://t.me/username"
        name = raw
    name = name[1:] if name.startswith("@") else name
    if not USERNAME_RE.match(name):
        return None, f"«{name[:40]}» не похоже на username канала (латиница, цифры, _; 4-32 символа)"
    return name, None


class ChannelListener:
    def __init__(self, tg: TelegramService, pipeline, repo, channel_configs, *, catchup_limit: int = 50,
                 poll_interval_sec: int = 300, retry_interval_sec: int = 600):
        self.tg = tg
        self.pipeline = pipeline
        self.repo = repo
        self.channel_configs = channel_configs  # yaml seed list
        self.catchup_limit = catchup_limit
        self.poll_interval = poll_interval_sec
        self.retry_interval = retry_interval_sec
        self.refs: list[ChannelRef] = []
        self._enabled: set[int] = set()
        self._click: dict[int, bool] = {}
        self._lock = asyncio.Lock()   # guards registry mutations
        self._tasks: set[asyncio.Task] = set()
        self._errors: dict[tuple[int, int], int] = {}

    # ------------------------------------------------------------- registry state

    async def _load_state(self) -> None:
        rows = await self.repo.list_channels()
        self._enabled = {r.tg_id for r in rows if r.enabled}
        self._click = {r.tg_id: bool(r.click_callbacks) for r in rows}

    def click_allowed(self, tg_id: int) -> bool:
        """Live 'may the resolver press this channel's callback button' flag.
        Unknown, deleted or disabled channels are never clicked."""
        return tg_id in self._enabled and self._click.get(tg_id, False)

    def is_enabled(self, tg_id: int) -> bool:
        return tg_id in self._enabled

    def _add_ref(self, ref: ChannelRef) -> None:
        self.refs = [r for r in self.refs if r.peer_id != ref.peer_id] + [ref]

    async def _deleted(self) -> list[str]:
        val = (await self.repo.get_settings()).get(DELETED_KEY)
        return [x for x in val if isinstance(x, str)] if isinstance(val, list) else []

    async def _deleted_ids(self) -> list[int]:
        val = (await self.repo.get_settings()).get(DELETED_IDS_KEY)
        return [x for x in val if isinstance(x, int) and not isinstance(x, bool)] if isinstance(val, list) else []

    async def seed_channels(self) -> int:
        """Insert yaml channels that the DB does not know yet. A channel that exists
        (even disabled) or was deleted from the bot (tombstone = resolved peer id) is
        never re-created/re-enabled. Caller holds ``_lock``."""
        cfgs = list(self.channel_configs or [])
        rows = await self.repo.list_channels()
        settings = await self.repo.get_settings()
        if not settings.get(SEEDED_KEY):
            # one-time registry migration: rows from the pre-registry schema are all
            # enabled with click_callbacks=1; bring them in line with the current yaml
            by_cfg_name = {c.username.lower(): c for c in cfgs}
            for row in rows:
                cfg = by_cfg_name.get((row.username or "").lower())
                if cfg is None:
                    if row.enabled:
                        await self.repo.set_channel_flags(row.tg_id, enabled=False)
                elif not getattr(cfg, "click_callbacks", True):
                    await self.repo.set_channel_flags(row.tg_id, click_callbacks=False)
            await self.repo.set_setting(SEEDED_KEY, True)
            rows = await self.repo.list_channels()
        if not cfgs:
            return 0
        legacy_deleted = {d.lower() for d in await self._deleted()}
        tombstones = set(await self._deleted_ids())
        known = {r.username.lower() for r in rows} | legacy_deleted
        todo = [c for c in cfgs if c.username.lower() not in known]
        if not todo:
            return 0
        by_cfg = {c.username.lower(): c for c in todo}
        known_ids = {r.tg_id for r in rows}
        added = 0
        for ref in await self.tg.resolve_channels(todo):
            if ref.peer_id in known_ids or ref.peer_id in tombstones:
                continue
            cfg = by_cfg.get(ref.username.lower())
            await self.repo.upsert_channel(ref.peer_id, ref.username, ref.title, enabled=True,
                                           click_callbacks=getattr(cfg, "click_callbacks", True))
            self._add_ref(ref)
            added += 1
            log.info("seeded channel @%s (%s)", ref.username, ref.title)
        return added

    async def _resolve_enabled_missing(self) -> int:
        have = {r.peer_id for r in self.refs}
        missing = [ChannelConfig(username=r.username) for r in await self.repo.list_channels()
                   if r.enabled and r.tg_id not in have]
        if not missing:
            return 0
        added = 0
        for ref in await self.tg.resolve_channels(missing):
            await self.repo.upsert_channel(ref.peer_id, ref.username, ref.title)
            self._add_ref(ref)
            added += 1
            log.info("monitoring @%s (%s)", ref.username, ref.title)
        return added

    async def setup(self) -> None:
        """Seed/resolve/load the registry. Run it to completion before the bot starts."""
        async with self._lock:
            await self.seed_channels()
            await self._resolve_enabled_missing()
            await self._load_state()
        if not self._enabled:
            log.warning("no enabled channels: add some from the bot")
        self.tg.client.add_event_handler(self._on_message, events.NewMessage())

    async def reresolve_missing(self) -> int:
        """Retry channels that failed to resolve at startup (FloodWait, temporary errors)."""
        async with self._lock:
            added = await self.seed_channels()
            added += await self._resolve_enabled_missing()
            await self._load_state()
            return added

    async def _on_message(self, event) -> None:
        if event.chat_id not in self._enabled:
            return  # not a monitored channel (handler has no chats= filter)
        # do not block Telethon's update loop with JEV/LLM/clicks
        try:
            post = self.tg.to_post(event.message)
        except Exception:
            log.exception("ERROR parsing event message")  # the poll will retry it
            return
        task = asyncio.create_task(self.pipeline.process_post(post))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------- bot-facing API

    async def add_channel(self, text: str) -> tuple[bool, str]:
        """Add a public channel/supergroup by '@name', 'name' or a t.me link. Never joins."""
        name, err = parse_channel_username(text)
        if err:
            return False, err
        async with self._lock:
            try:
                ref = await self.tg.lookup_public_channel(name)
            except ChannelLookupError as e:
                return False, str(e)
            except errors.FloodWaitError as e:
                return False, f"Telegram просит подождать {e.seconds} с"
            except Exception as e:
                log.exception("ERROR resolving @%s", name)
                return False, f"Не удалось проверить канал: {type(e).__name__}"
            rows = {r.tg_id: r for r in await self.repo.list_channels()}
            existing = rows.get(ref.peer_id)
            if existing is not None and existing.enabled:
                return False, f"Канал @{ref.username} уже отслеживается"
            await self.repo.upsert_channel(ref.peer_id, ref.username, ref.title)
            await self.repo.set_channel_flags(ref.peer_id, enabled=True)
            deleted = await self._deleted()
            kept = [d for d in deleted if d.lower() != ref.username.lower()]
            if kept != deleted:
                await self.repo.set_setting(DELETED_KEY, kept)
            ids = await self._deleted_ids()
            if ref.peer_id in ids:
                await self.repo.set_setting(DELETED_IDS_KEY, [i for i in ids if i != ref.peer_id])
            self._add_ref(ref)
            await self._load_state()
        log.info("channel added from bot: @%s (%s)", ref.username, ref.title)
        return True, f"Канал @{ref.username} ({ref.title}) добавлен"

    async def set_enabled(self, tg_id: int, enabled: bool) -> bool:
        """Start/stop monitoring a channel. False if the channel is unknown."""
        async with self._lock:
            if not await self.repo.set_channel_flags(tg_id, enabled=enabled):
                return False
            await self._load_state()
            if enabled and all(r.peer_id != tg_id for r in self.refs):
                try:
                    await self._resolve_enabled_missing()  # else reresolve_missing retries later
                except Exception:
                    log.exception("ERROR resolving re-enabled channel %s", tg_id)
        return True

    async def set_click(self, tg_id: int, allowed: bool) -> bool:
        async with self._lock:
            if not await self.repo.set_channel_flags(tg_id, click_callbacks=allowed):
                return False
            await self._load_state()
        return True

    async def remove_channel(self, tg_id: int) -> bool:
        """Forget a channel (messages/jobs stay); yaml seeding will not bring it back."""
        async with self._lock:
            row = next((r for r in await self.repo.list_channels() if r.tg_id == tg_id), None)
            if row is None:
                return False
            ids = await self._deleted_ids()
            if tg_id not in ids:
                await self.repo.set_setting(DELETED_IDS_KEY, ids + [tg_id])
            await self.repo.delete_channel(tg_id)
            self.refs = [r for r in self.refs if r.peer_id != tg_id]
            await self._load_state()
        return True

    async def channels_overview(self) -> list[dict]:
        return [{"tg_id": r.tg_id, "username": r.username, "title": r.title, "enabled": bool(r.enabled),
                 "click_callbacks": bool(r.click_callbacks), "last_message_id": int(r.last_message_id or 0)}
                for r in await self.repo.list_channels()]

    # ------------------------------------------------------------- polling

    async def poll_once(self) -> int:
        processed = 0
        try:
            await self._load_state()
        except Exception:
            log.exception("ERROR loading channel flags")
        for ref in list(self.refs):
            if ref.peer_id not in self._enabled:
                continue
            try:
                last = await self.repo.get_last_message_id(ref.peer_id)
                for msg in await self.tg.fetch_new(ref, last, self.catchup_limit):
                    if not self.is_enabled(ref.peer_id):
                        break  # disabled/deleted mid-batch: leave the rest (cursor not advanced)
                    key = (ref.peer_id, msg.id)
                    try:
                        post = self.tg.to_post(msg)
                    except Exception:
                        log.exception("ERROR parsing @%s/%s", ref.username, msg.id)
                        outcome = "error"  # counted below; skipped after MAX_POST_ERRORS
                    else:
                        outcome = await self.pipeline.process_post(post)
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
        await self.run_loop()

    async def run_loop(self) -> None:
        """Poll/retry forever; ``setup()`` must have completed before."""
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
