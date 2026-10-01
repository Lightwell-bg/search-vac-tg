from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import BotHandlers
from src.bot.menu import (
    MenuHandlers, MenuStates, channels_keyboard, channels_text, confirm_delete_keyboard, interval_keyboard,
    main_keyboard,
    main_text, parse_menu_callback, thresholds_keyboard, thresholds_text,
)


def rs_fake(**kw):
    base = dict(notify_score=60, high_fit_score=80, show_paid_contact=False, notifications_paused=False,
                openrouter_model="google/x", poll_interval_sec=120, set=AsyncMock())
    base.update(kw)
    return SimpleNamespace(**base)


CHS = [
    {"tg_id": 100, "username": "chan_a", "title": "<b>x</b>", "enabled": True, "click_callbacks": True,
     "last_message_id": 1},
    {"tg_id": -200, "username": "chan_b", "title": "B", "enabled": False, "click_callbacks": False,
     "last_message_id": 0},
]


def texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def datas(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]


def test_main_menu_reflects_state():
    rs = rs_fake()
    t = main_text(rs, CHS)
    assert "▶️ Уведомления включены" in t and "1 активных" in t and "скрывать" in t
    assert "⏸ Пауза" in texts(main_keyboard(rs)) and "💰 Платные: выкл" in texts(main_keyboard(rs))
    rs = rs_fake(notifications_paused=True, show_paid_contact=True)
    assert "⏸ Уведомления на паузе" in main_text(rs, CHS) and "показывать" in main_text(rs, CHS)
    assert "▶️ Продолжить" in texts(main_keyboard(rs)) and "💰 Платные: вкл" in texts(main_keyboard(rs))


def test_model_and_title_are_escaped():
    assert "<b>x</b>" not in channels_text(CHS) and "&lt;b&gt;x&lt;/b&gt;" in channels_text(CHS)
    assert "<script>" not in main_text(rs_fake(openrouter_model="<script>/a"), CHS)


def test_channels_keyboard_labels_and_lengths():
    kb = channels_keyboard(CHS)
    t = texts(kb)
    assert "✅ @chan_a" in t and "⏸ @chan_b" in t
    assert "👆 клик: да" in t and "👆 клик: нет" in t
    assert "ch:t:100" in datas(kb) and "ch:d:-200" in datas(kb) and "ch:add" in datas(kb)
    for kbd in (kb, main_keyboard(rs_fake()), thresholds_keyboard(rs_fake()), confirm_delete_keyboard(-10**15)):
        assert all(len(d.encode()) < 64 for d in datas(kbd))


def test_thresholds_keyboard():
    kb = thresholds_keyboard(rs_fake())
    assert {"th:n:+5", "th:n:-5", "th:h:+5", "th:h:-5"} <= set(datas(kb))
    assert "60" in thresholds_text(rs_fake()) and "80" in thresholds_text(rs_fake())


def test_main_menu_shows_interval_and_button():
    rs = rs_fake(poll_interval_sec=300)
    assert "⏱ Проверка каналов: каждые 5 мин" in main_text(rs, CHS)
    assert "⏱ Период проверки" in texts(main_keyboard(rs))


def test_interval_keyboard_marks_current():
    kb = interval_keyboard(rs_fake(poll_interval_sec=300))
    assert texts(kb)[:7] == ["1 мин", "2 мин", "✅ 5 мин", "10 мин", "15 мин", "30 мин", "1 ч"]
    assert "⬅️ Назад" in texts(kb) and "pi:300" in datas(kb) and "pi:3600" in datas(kb)
    assert all(len(d.encode()) < 64 for d in datas(kb))


async def test_interval_callback_sets_value():
    rs = rs_fake()
    menu = make_menu(rs, make_listener())
    cb = make_cb("pi:300")
    await menu.on_callback(cb, FakeState())
    rs.set.assert_awaited_once_with("poll_interval_sec", 300)
    cb.message.edit_text.assert_awaited()
    rs.set.reset_mock()
    await menu.on_callback(make_cb("pi:77"), FakeState())
    await menu.on_callback(make_cb("pi:abc"), FakeState())
    rs.set.assert_not_awaited()
    cb2 = make_cb("m:pi")
    await menu.on_callback(cb2, FakeState())
    assert "Сейчас" in cb2.message.edit_text.await_args.args[0]


def test_parse_menu_callback():
    assert parse_menu_callback("ch:t:5") == ("ch", "t", "5")
    assert parse_menu_callback("x:1") is None and parse_menu_callback(None) is None
    assert parse_menu_callback("ch") is None


# ------------------------------------------------------------------ handlers


class FakeState:
    def __init__(self):
        self.cleared = 0
        self.state = None

    async def clear(self):
        self.cleared += 1
        self.state = None

    async def set_state(self, s):
        self.state = s


def make_cb(data):
    cb = MagicMock()
    cb.data = data
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    return cb


def make_msg(text=None, forward_username=None):
    m = MagicMock()
    m.text = text
    m.answer = AsyncMock()
    m.forward_origin = SimpleNamespace(chat=SimpleNamespace(username=forward_username)) \
        if forward_username else None
    return m


def make_listener(chs=None, add=(True, "Канал добавлен")):
    lst = MagicMock()
    lst.channels_overview = AsyncMock(return_value=chs if chs is not None else [dict(c) for c in CHS])
    lst.add_channel = AsyncMock(return_value=add)
    lst.set_enabled = AsyncMock(return_value=True)
    lst.set_click = AsyncMock(return_value=True)
    lst.remove_channel = AsyncMock(return_value=True)
    return lst


def make_menu(rs=None, lst=None):
    repo = MagicMock()
    repo.get_stats = AsyncMock(return_value={"messages_received": 3})
    return MenuHandlers(repo, rs, lst)


async def test_toggle_pause_and_paid():
    rs = rs_fake()
    menu = make_menu(rs, make_listener())
    cb = make_cb("m:pause")
    await menu.on_callback(cb, FakeState())
    rs.set.assert_awaited_once_with("notifications_paused", True)
    cb.answer.assert_awaited()
    cb2 = make_cb("m:paid")
    await menu.on_callback(cb2, FakeState())
    rs.set.assert_awaited_with("show_paid_contact", True)


async def test_threshold_plus_five_and_error_alert():
    rs = rs_fake()
    menu = make_menu(rs, make_listener())
    await menu.on_callback(make_cb("th:n:+5"), FakeState())
    rs.set.assert_awaited_once_with("notify_score", 65)
    rs.set = AsyncMock(side_effect=ValueError("Порог не может быть выше"))
    cb = make_cb("th:h:-5")
    await menu.on_callback(cb, FakeState())
    cb.answer.assert_awaited_once_with("Порог не может быть выше", show_alert=True)


async def test_bad_callback_data_is_silent():
    rs = rs_fake()
    menu = make_menu(rs, make_listener())
    for data in ("ch:t:abc", "th:n:zz", "th:x:+5", "m:unknown", "ch:t:999"):
        cb = make_cb(data)
        await menu.on_callback(cb, FakeState())
        cb.answer.assert_awaited()
    rs.set.assert_not_awaited()


async def test_unavailable_without_runtime():
    menu = make_menu(None, None)
    cb = make_cb("m:pause")
    await menu.on_callback(cb, FakeState())
    assert cb.answer.await_args.kwargs.get("show_alert") is True
    msg = make_msg("/menu")
    await menu.cmd_menu(msg, FakeState())
    assert "Недоступно" in msg.answer.await_args.args[0]


async def test_delete_requires_confirmation():
    lst = make_listener()
    menu = make_menu(rs_fake(), lst)
    cb = make_cb("ch:d:100")
    await menu.on_callback(cb, FakeState())
    lst.remove_channel.assert_not_awaited()
    assert "Удалить" in cb.message.edit_text.await_args.args[0]
    await menu.on_callback(make_cb("ch:dy:100"), FakeState())
    lst.remove_channel.assert_awaited_once_with(100)


async def test_channel_toggles():
    lst = make_listener()
    menu = make_menu(rs_fake(), lst)
    await menu.on_callback(make_cb("ch:t:100"), FakeState())
    lst.set_enabled.assert_awaited_once_with(100, False)
    await menu.on_callback(make_cb("ch:c:-200"), FakeState())
    lst.set_click.assert_awaited_once_with(-200, True)


async def test_add_channel_flow():
    lst = make_listener(add=(False, "Канал не найден"))
    menu = make_menu(rs_fake(), lst)
    st = FakeState()
    await menu.on_callback(make_cb("ch:add"), st)
    assert st.state == MenuStates.waiting_channel
    msg = make_msg("@nope")
    await menu.on_channel_input(msg, st)
    lst.add_channel.assert_awaited_once_with("@nope")
    assert st.state == MenuStates.waiting_channel  # stays for a retry
    lst.add_channel.return_value = (True, "Канал @ok добавлен")
    msg2 = make_msg("@ok")
    await menu.on_channel_input(msg2, st)
    assert st.state is None and msg2.answer.await_count == 2  # result + channels screen


async def test_add_channel_forward_and_non_text():
    lst = make_listener()
    menu = make_menu(rs_fake(), lst)
    st = FakeState()
    await menu.on_channel_input(make_msg(None, forward_username="fwd_chan"), st)
    lst.add_channel.assert_awaited_once_with("@fwd_chan")
    lst.add_channel.reset_mock()
    msg = make_msg(None)
    await menu.on_channel_input(msg, st)
    lst.add_channel.assert_not_awaited()
    assert "Нужен текст" in msg.answer.await_args.args[0]


async def test_model_flow():
    rs = rs_fake(set=AsyncMock(side_effect=ValueError("bad")))
    menu = make_menu(rs, make_listener())
    st = FakeState()
    await menu.on_callback(make_cb("m:md"), st)
    assert st.state == MenuStates.waiting_model
    msg = make_msg("bad model")
    await menu.on_model_input(msg, st)
    assert st.state == MenuStates.waiting_model and "bad" in msg.answer.await_args.args[0]
    rs.set = AsyncMock()
    await menu.on_model_input(make_msg("a/b"), st)
    rs.set.assert_awaited_once_with("openrouter_model", "a/b")
    assert st.state is None


@pytest.mark.parametrize("name", ["cmd_menu", "cmd_stats", "cmd_cancel", "cmd_channels", "cmd_start"])
async def test_commands_clear_state(name):
    menu = make_menu(rs_fake(), make_listener())
    st = FakeState()
    st.state = MenuStates.waiting_channel
    await getattr(menu, name)(make_msg("/x"), st)
    assert st.cleared == 1 and st.state is None


async def test_menu_callback_clears_state():
    menu = make_menu(rs_fake(), make_listener())
    st = FakeState()
    st.state = MenuStates.waiting_model
    await menu.on_callback(make_cb("m:main"), st)
    assert st.state is None


def test_router_wiring_has_owner_middleware():
    settings = SimpleNamespace(owner_telegram_id=42)
    h = BotHandlers(settings, MagicMock(), None, {}, runtime_settings=rs_fake(), listener=make_listener())
    assert h.menu.runtime_settings is not None
    assert h.router.message.outer_middleware and h.router.callback_query.outer_middleware


# ---------------------------------------------------------------- custom interval / check now

from datetime import datetime, timezone  # noqa: E402
import asyncio  # noqa: E402

from src.bot.menu import interval_text  # noqa: E402


def test_interval_keyboard_custom_and_now_buttons():
    kb = interval_keyboard(rs_fake(poll_interval_sec=500 * 60))
    assert "✏️ Своё значение" in texts(kb) and "🔄 Проверить сейчас" in texts(kb)
    assert "pi:custom" in datas(kb) and "pi:now" in datas(kb)
    assert not any(t.startswith("✅") for t in texts(kb))  # custom value: no preset is marked


def test_interval_text_formats_and_last_poll():
    rs = rs_fake(poll_interval_sec=500 * 60)
    assert "каждые 8 ч 20 мин" in interval_text(rs)
    lst = SimpleNamespace(last_poll_at=datetime(2026, 10, 1, 14, 5, tzinfo=timezone.utc), last_poll_new=3)
    rs.timezone = "Europe/Sofia"  # EEST, UTC+3
    assert "Последняя проверка: 17:05 (3 новых)" in interval_text(rs, lst)
    assert "каждые 90 с" in main_text(rs_fake(poll_interval_sec=90), CHS)


async def test_custom_interval_flow():
    rs = rs_fake()
    menu = make_menu(rs, make_listener())
    st = FakeState()
    await menu.on_callback(make_cb("pi:custom"), st)
    assert st.state == MenuStates.waiting_interval
    bad = make_msg("abc")
    await menu.on_interval_input(bad, st)
    rs.set.assert_not_awaited()
    assert st.state == MenuStates.waiting_interval
    msg = make_msg("500")
    rs.set.side_effect = lambda k, v: setattr(rs, k, v)
    await menu.on_interval_input(msg, st)
    rs.set.assert_awaited_once_with("poll_interval_sec", 30000)
    assert st.state is None
    assert "каждые 8 ч 20 мин" in msg.answer.await_args_list[0].args[0]
    assert msg.answer.await_count == 2


async def test_check_now_triggers_poll_and_reports():
    rs = rs_fake()
    lst = make_listener()
    done = asyncio.Event()
    done.set()
    lst.request_poll_now = MagicMock(return_value=done)
    lst.last_poll_new = 4
    lst.last_poll_at = None
    menu = make_menu(rs, lst)
    cb = make_cb("pi:now")
    cb.message.answer = AsyncMock()
    await menu.on_callback(cb, FakeState())
    cb.answer.assert_awaited_with("Проверяю каналы…")
    cb.message.answer.assert_awaited_with("Проверено: 4 новых постов", parse_mode=None)
    rs.set.assert_not_awaited()


async def test_check_now_when_already_running():
    lst = make_listener()
    lst.request_poll_now = MagicMock(return_value=None)
    cb = make_cb("pi:now")
    await make_menu(rs_fake(), lst).on_callback(cb, FakeState())
    assert "уже идёт" in cb.answer.await_args.args[0]
