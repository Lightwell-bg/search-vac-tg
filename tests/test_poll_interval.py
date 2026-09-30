"""Listener reads the poll interval live and wakes up when it changes."""
import asyncio
from types import SimpleNamespace

from src.telegram.listener import ChannelListener


def make_listener(interval=300):
    return ChannelListener(SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), [],
                           poll_interval_sec=interval, retry_interval_sec=10**6)


async def test_set_poll_interval_wakes_waiting_loop():
    lst = make_listener(3600)
    waiter = asyncio.create_task(lst._wait_next_poll())
    await asyncio.sleep(0)
    assert not waiter.done()
    lst.set_poll_interval(60)
    await asyncio.wait_for(waiter, timeout=1)
    assert lst.poll_interval == 60


async def test_wait_uses_current_interval_as_timeout(monkeypatch):
    lst = make_listener(300)
    seen = []

    async def fake_wait_for(aw, timeout):
        seen.append(timeout)
        aw.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    await lst._wait_next_poll()
    lst.set_poll_interval(900)
    await lst._wait_next_poll()  # wake flag from the change is cleared, waits the new interval
    assert seen == [300, 900] and not lst._wake.is_set()


async def test_run_loop_polls_again_after_change():
    lst = make_listener(3600)
    polls = []

    async def poll_once():
        polls.append(lst.poll_interval)
        if len(polls) == 2:
            raise asyncio.CancelledError
        return 0

    lst.poll_once = poll_once
    task = asyncio.create_task(lst.run_loop())
    await asyncio.sleep(0.01)
    assert polls == [3600]
    lst.set_poll_interval(60)
    try:
        await asyncio.wait_for(task, timeout=1)
    except asyncio.CancelledError:
        pass
    assert polls == [3600, 60]


async def test_request_poll_now_polls_immediately_keeping_interval():
    lst = make_listener(3600)
    polls = []

    async def poll_once():
        polls.append(1)
        return 2

    lst.poll_once = poll_once
    task = asyncio.create_task(lst.run_loop())
    await asyncio.sleep(0.01)
    assert polls == [1] and lst.last_poll_new == 2 and lst.last_poll_at is not None
    done = lst.request_poll_now()
    assert done is not None
    await asyncio.wait_for(done.wait(), timeout=1)
    assert len(polls) == 2 and lst.poll_interval == 3600
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_request_poll_now_refused_while_polling():
    lst = make_listener(3600)
    gate = asyncio.Event()

    async def poll_once():
        await gate.wait()
        return 0

    lst.poll_once = poll_once
    task = asyncio.create_task(lst.run_loop())
    await asyncio.sleep(0.01)
    assert lst.request_poll_now() is None
    gate.set()
    await asyncio.sleep(0.01)
    assert lst.request_poll_now() is not None
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
