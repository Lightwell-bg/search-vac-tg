"""Channel registry: seeding, add_channel parsing/validation, live flags, event filtering."""
from types import SimpleNamespace

import pytest
from telethon import errors

from src.config import ChannelConfig
from src.telegram.client import ChannelLookupError, ChannelRef
from src.telegram.contact_resolver import CallbackAnswer, ContactResolver
from src.telegram.listener import ChannelListener, parse_channel_username
from src.telegram.parser import ButtonInfo

from .helpers import make_post
from .test_listener import FakeTg, ScriptedPipeline


class RegTg(FakeTg):
    """Fake TelegramService: `known` maps username(lower) -> ChannelRef or an exception."""

    def __init__(self, known=None):
        super().__init__([])
        self.known = known or {}
        self.lookups: list[str] = []
        self.resolved: list[list[str]] = []
        self.handlers: list = []
        self.client = SimpleNamespace(add_event_handler=lambda *a, **k: self.handlers.append(a))

    async def lookup_public_channel(self, username):
        self.lookups.append(username)
        r = self.known.get(username.lower())
        if r is None:
            raise ChannelLookupError(f"@{username} не найден")
        if isinstance(r, BaseException):
            raise r
        return r

    async def resolve_channels(self, configs):
        self.resolved.append([c.username for c in configs])
        return [self.known[c.username.lower()] for c in configs
                if isinstance(self.known.get(c.username.lower()), ChannelRef)]


def ref(name, pid, title=None):
    return ChannelRef(name, pid, object(), title or name.title())


def make(repo, tg, cfgs=()):
    return ChannelListener(tg, ScriptedPipeline(), repo, list(cfgs), catchup_limit=50)


# ------------------------------------------------------------------ parsing


@pytest.mark.parametrize("text,expected", [
    ("@freelance_jobs", "freelance_jobs"), ("freelance_jobs", "freelance_jobs"),
    ("  https://t.me/freelance_jobs  ", "freelance_jobs"), ("t.me/freelance_jobs", "freelance_jobs"),
    ("http://t.me/freelance_jobs/123", "freelance_jobs"), ("https://t.me/freelance_jobs?start=1", "freelance_jobs"),
])
def test_parse_ok(text, expected):
    assert parse_channel_username(text) == (expected, None)


@pytest.mark.parametrize("text", [
    "", "   ", "https://t.me/+AbCdEf123", "t.me/joinchat/AAAA", "https://t.me/c/123456/7",
    "abc", "1channel", "bad name", "a" * 33, "https://example.com/chan", "@@x_y_z_w",
])
def test_parse_rejects(text):
    name, err = parse_channel_username(text)
    assert name is None and err


# ------------------------------------------------------------------ add_channel


async def test_add_public_channel(repo):
    tg = RegTg({"newchan": ref("newchan", -1005, "New Chan")})
    lst = make(repo, tg)
    await repo.set_setting("deleted_channels", ["newchan", "other"])
    ok, msg = await lst.add_channel("https://t.me/NewChan")
    assert ok and msg == "Канал @newchan (New Chan) добавлен"
    assert tg.lookups == ["NewChan"]
    row = await repo.get_channel_by_username("newchan")
    assert row.tg_id == -1005 and row.enabled and row.click_callbacks
    assert (await repo.get_settings())["deleted_channels"] == ["other"]
    assert [r.peer_id for r in lst.refs] == [-1005] and lst.is_enabled(-1005)
    ok, msg = await lst.add_channel("@newchan")
    assert not ok and "уже" in msg


async def test_add_reenables_disabled_channel(repo):
    await repo.upsert_channel(-1005, "newchan", "x", enabled=False)
    lst = make(repo, RegTg({"newchan": ref("newchan", -1005)}))
    assert (await lst.add_channel("newchan"))[0] is True
    assert lst.is_enabled(-1005)


@pytest.mark.parametrize("text", ["https://t.me/+abcdef", "t.me/joinchat/AAAA", "t.me/c/1/2", "x"])
async def test_add_rejects_bad_input_without_lookup(repo, text):
    tg = RegTg()
    ok, msg = await make(repo, tg).add_channel(text)
    assert not ok and msg and tg.lookups == []


@pytest.mark.parametrize("reason", ["это пользователь", "это бот", "канал приватный"])
async def test_add_rejects_user_bot_private(repo, reason):
    tg = RegTg({"someone": ChannelLookupError(f"@someone: {reason}")})
    lst = make(repo, tg)
    ok, msg = await lst.add_channel("@someone")
    assert not ok and reason in msg
    assert await lst.channels_overview() == []


async def test_add_not_found(repo):
    ok, msg = await make(repo, RegTg()).add_channel("@ghostchan")
    assert not ok and "не найден" in msg


async def test_add_floodwait(repo):
    tg = RegTg({"slowchan": errors.FloodWaitError(request=None, capture=42)})
    ok, msg = await make(repo, tg).add_channel("@slowchan")
    assert not ok and msg == "Telegram просит подождать 42 с"


async def test_add_unexpected_error(repo):
    tg = RegTg({"brokenchan": RuntimeError("boom")})
    ok, msg = await make(repo, tg).add_channel("@brokenchan")
    assert not ok and "RuntimeError" in msg


# ------------------------------------------------------------------ seeding


async def test_seeding_inserts_only_unknown_channels(repo):
    await repo.upsert_channel(-1, "existing", "E", enabled=False)
    tg = RegTg({"existing": ref("existing", -1), "fresh": ref("fresh", -2), "gone": ref("gone", -3)})
    await repo.set_setting("deleted_channels", ["gone"])
    cfgs = [ChannelConfig("existing"), ChannelConfig("Fresh", click_callbacks=False), ChannelConfig("gone")]
    lst = make(repo, tg, cfgs)
    await lst.setup()
    assert tg.resolved[0] == ["Fresh"]  # only the unknown one is resolved for seeding
    rows = {r.username: r for r in await repo.list_channels()}
    assert set(rows) == {"existing", "fresh"}
    assert rows["existing"].enabled is False           # not re-enabled
    assert rows["fresh"].enabled and rows["fresh"].click_callbacks is False
    assert [r.username for r in lst.refs] == ["fresh"]  # disabled channel is not monitored
    assert lst.is_enabled(-2) and not lst.is_enabled(-1)
    assert len(tg.handlers) == 1                        # one handler, no chats filter


async def test_seeding_is_not_repeated_after_restart(repo):
    tg = RegTg({"fresh": ref("fresh", -2)})
    cfgs = [ChannelConfig("fresh")]
    await make(repo, tg, cfgs).setup()
    await repo.set_channel_flags(-2, enabled=False)
    lst2 = make(repo, tg, cfgs)
    await lst2.setup()
    assert not lst2.is_enabled(-2) and lst2.refs == []


async def test_deleted_channel_not_resurrected(repo):
    tg = RegTg({"fresh": ref("fresh", -2)})
    cfgs = [ChannelConfig("fresh")]
    lst = make(repo, tg, cfgs)
    await lst.setup()
    assert await lst.remove_channel(-2) is True
    assert await lst.remove_channel(-2) is False
    assert (await repo.get_settings())["deleted_channel_ids"] == [-2]
    lst2 = make(repo, tg, cfgs)
    await lst2.setup()
    assert await repo.list_channels() == [] and lst2.refs == []


async def test_one_time_yaml_click_flag_carried_to_migrated_row(repo):
    await repo.upsert_channel(-1, "old", "Old")  # migrated row: click_callbacks defaulted to True
    lst = make(repo, RegTg({"old": ref("old", -1)}), [ChannelConfig("old", click_callbacks=False)])
    await lst.setup()
    assert lst.click_allowed(-1) is False
    await lst.set_click(-1, True)
    lst2 = make(repo, RegTg({"old": ref("old", -1)}), [ChannelConfig("old", click_callbacks=False)])
    await lst2.setup()
    assert lst2.click_allowed(-1) is True  # marker set: yaml no longer overrides the bot


# ------------------------------------------------------------------ live flags / events


async def test_event_filtering_by_enabled_set(repo):
    tg = RegTg({"chan": ref("chan", -10)})
    lst = make(repo, tg)
    tg.to_post = lambda m: make_post("t", m.id, -10, "chan")
    await lst.add_channel("chan")
    made = []
    lst.pipeline = SimpleNamespace(process_post=lambda post: _record(made, post))
    ev = lambda cid: SimpleNamespace(chat_id=cid, message=SimpleNamespace(id=5))
    await lst._on_message(ev(-99))   # unknown chat: ignored
    await lst._on_message(ev(-10))
    import asyncio
    await asyncio.gather(*list(lst._tasks))
    assert len(made) == 1
    await lst.set_enabled(-10, False)
    await lst._on_message(ev(-10))
    assert len(lst._tasks) == 0 and len(made) == 1


async def _record(made, post):
    made.append(post)
    return "notified"


async def test_set_enabled_removes_from_polling(repo):
    tg = RegTg({"chan": ref("chan", -10)})
    lst = make(repo, tg)
    await lst.add_channel("chan")
    tg.ids = [1, 2]
    tg.to_post = lambda m: make_post("t", m.id, -10, "chan")
    assert await lst.poll_once() == 2
    tg.ids = [1, 2, 3]
    await lst.set_enabled(-10, False)
    assert await lst.poll_once() == 0
    assert (await lst.channels_overview())[0]["enabled"] is False
    await lst.set_enabled(-10, True)
    assert await lst.poll_once() == 1
    assert await lst.set_enabled(-999, True) is False


async def test_overview_fields(repo):
    lst = make(repo, RegTg({"chan": ref("chan", -10, "Title")}))
    await lst.add_channel("chan")
    await repo.set_last_message_id(-10, 12)
    assert await lst.channels_overview() == [
        {"tg_id": -10, "username": "chan", "title": "Title", "enabled": True,
         "click_callbacks": True, "last_message_id": 12}]


async def test_click_policy_is_live(repo):
    from .helpers import FakeActions
    lst = make(repo, RegTg({"chan": ref("chan", -10)}))
    await lst.add_channel("chan")
    actions = FakeActions(CallbackAnswer("Контакт: @client_user"))
    resolver = ContactResolver(actions, set(), click_policy=lambda post: lst.click_allowed(post.channel_tg_id))
    buttons = [ButtonInfo("Получить контакт", "callback", data_hex="00")]
    post = make_post("Нужен разработчик бота", 1, -10, "chan", buttons)
    await lst.set_click(-10, False)
    assert lst.click_allowed(-10) is False
    await resolver.resolve(post, [])
    assert actions.clicks == 0
    await lst.set_click(-10, True)
    await resolver.resolve(post, [])
    assert actions.clicks == 1
    assert lst.click_allowed(-12345) is False  # unknown channels are never clicked


async def test_concurrent_adds_do_not_duplicate(repo):
    import asyncio
    lst = make(repo, RegTg({"chan": ref("chan", -10)}))
    results = await asyncio.gather(lst.add_channel("chan"), lst.add_channel("@chan"))
    assert sorted(ok for ok, _ in results) == [False, True]
    assert len(await repo.list_channels()) == 1
