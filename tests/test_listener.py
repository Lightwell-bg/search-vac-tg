"""poll_once cursor policy: only a durably stored post moves last_message_id."""
from types import SimpleNamespace

import pytest

from src.telegram.client import ChannelRef
from src.telegram.listener import MAX_POST_ERRORS, ChannelListener

from .helpers import make_post

PEER = -1001


class FakeTg:
    def __init__(self, ids):
        self.ids = ids

    async def fetch_new(self, ref, after_id, limit):
        return [SimpleNamespace(id=i) for i in self.ids if i > after_id]

    def to_post(self, msg):
        return make_post(f"post {msg.id}", msg.id, PEER, "chan")


class ScriptedPipeline:
    """process_post returns scripted outcomes per message id (default 'notified')."""

    def __init__(self, script=None):
        self.script = script or {}
        self.calls: list[int] = []

    async def process_post(self, post):
        self.calls.append(post.message_id)
        outcomes = self.script.get(post.message_id, ["notified"])
        return outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]


async def make_listener(repo, ids, script=None):
    await repo.upsert_channel(PEER, "chan", "Chan")
    pipe = ScriptedPipeline(script)
    listener = ChannelListener(FakeTg(ids), pipe, repo, [], catchup_limit=50)
    listener.refs = [ChannelRef("chan", PEER, object(), "Chan")]
    return listener, pipe


async def test_all_ok_advances_to_last(repo):
    listener, pipe = await make_listener(repo, [1, 2, 3])
    assert await listener.poll_once() == 3
    assert await repo.get_last_message_id(PEER) == 3


@pytest.mark.parametrize("outcome", ["failed", "notified", "seen", "duplicate", "rule_rejected", "empty"])
async def test_stored_outcomes_advance_cursor(repo, outcome):
    listener, pipe = await make_listener(repo, [1, 2], {1: [outcome]})
    await listener.poll_once()
    assert await repo.get_last_message_id(PEER) == 2
    assert pipe.calls == [1, 2]


async def test_error_stops_batch_and_keeps_cursor(repo):
    listener, pipe = await make_listener(repo, [1, 2, 3], {2: ["error"]})
    assert await listener.poll_once() == 1
    assert await repo.get_last_message_id(PEER) == 1  # message 2 will be fetched again
    assert pipe.calls == [1, 2]  # message 3 not attempted in this poll


async def test_error_then_success_next_poll(repo):
    listener, pipe = await make_listener(repo, [1, 2, 3], {2: ["error", "notified"]})
    await listener.poll_once()
    assert await repo.get_last_message_id(PEER) == 1
    await listener.poll_once()
    assert await repo.get_last_message_id(PEER) == 3
    assert not listener._errors


async def test_poison_message_skipped_after_three_errors(repo, caplog):
    listener, pipe = await make_listener(repo, [1, 2, 3], {2: ["error"]})
    for _ in range(MAX_POST_ERRORS - 1):
        await listener.poll_once()
        assert await repo.get_last_message_id(PEER) == 1
    with caplog.at_level("ERROR", logger="listener"):
        await listener.poll_once()
    assert await repo.get_last_message_id(PEER) == 3  # skipped 2 and processed 3
    assert "skipping poison message" in caplog.text
    assert pipe.calls.count(2) == MAX_POST_ERRORS


async def test_one_channel_error_does_not_block_other_channels(repo):
    await repo.upsert_channel(-1002, "other", "Other")

    class TwoChanTg:
        async def fetch_new(self, ref, after_id, limit):
            base = 0 if ref.peer_id == PEER else 10
            return [SimpleNamespace(id=base + i, peer=ref.peer_id) for i in (1, 2) if base + i > after_id]

        def to_post(self, msg):
            return make_post(f"post {msg.id}", msg.id, msg.peer, "chan")

    pipe = ScriptedPipeline({1: ["error"]})
    listener = ChannelListener(TwoChanTg(), pipe, repo, [], catchup_limit=50)
    listener.refs = [ChannelRef("chan", PEER, object(), "Chan"), ChannelRef("other", -1002, object(), "Other")]
    await listener.poll_once()
    assert await repo.get_last_message_id(PEER) == 0
    assert await repo.get_last_message_id(-1002) == 12


async def test_reresolve_missing_registers_late_channel(repo):
    await repo.upsert_channel(PEER, "chan", "Chan")
    late = ChannelRef("late", -1003, object(), "Late")
    asked: list[list[str]] = []
    handlers: list = []

    class Tg(FakeTg):
        client = SimpleNamespace(add_event_handler=lambda *a, **k: handlers.append(a))

        async def resolve_channels(self, configs):
            asked.append([c.username for c in configs])
            return [late]

    cfgs = [SimpleNamespace(username="chan"), SimpleNamespace(username="late")]
    listener = ChannelListener(Tg([]), ScriptedPipeline(), repo, cfgs)
    listener.refs = [ChannelRef("chan", PEER, object(), "Chan")]
    assert await listener.reresolve_missing() == 1
    assert asked == [["late"]]  # only the unresolved channel is asked again
    assert [r.username for r in listener.refs] == ["chan", "late"] and len(handlers) == 1
    assert await listener.reresolve_missing() == 0 and asked == [["late"]]
