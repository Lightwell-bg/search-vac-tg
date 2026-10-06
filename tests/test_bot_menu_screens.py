"""Stage 16 part 2: status dashboard, settings submenu, backups screen, alerts toggle, welcome text."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.bot.journal_view import retention_keyboard
from src.bot.menu import (
    WELCOME_TEXT, back_keyboard, backup_keyboard, interval_keyboard, main_keyboard, main_text, settings_keyboard,
    settings_text, thresholds_keyboard, timezone_keyboard,
)
from tests.test_bot_menu import (
    CHS, FakeState, datas, make_cb, make_listener, make_menu, make_msg, rs_fake, texts,
)


def test_main_keyboard_layout():
    kb = main_keyboard(rs_fake())
    assert [[b.text for b in row] for row in kb.inline_keyboard] == [
        ["📜 Журнал", "📊 Статистика"], ["🔄 Проверить сейчас", "⏸ Пауза"],
        ["📡 Каналы", "👤 Профиль"], ["⚙️ Настройки"]]
    assert {"m:now", "m:set", "m:ch", "m:pr", "m:st", "m:pause", "j:all:d:0"} == set(datas(kb))


def test_main_text_header_and_summary():
    t = main_text(rs_fake(), CHS)
    assert "🤖 <b>Поиск заказов</b> · v" in t and "🎯 Порог 60 · 🔥 высокий 80" in t
    assert "🧠 Модель: <code>google/x</code>" in t and "🕒 Часовой пояс: Europe/Sofia" in t


def test_main_text_poll_failures_and_backup():
    rs = rs_fake(timezone="Europe/Sofia", alerts_enabled=False)
    ok = SimpleNamespace(poll_failures=0, last_poll_error=None, last_poll_new=3,
                         last_poll_at=datetime(2026, 10, 6, 11, 32, tzinfo=timezone.utc))
    t = main_text(rs, CHS, ok, {"last_at": datetime(2026, 10, 6, 1, 0, tzinfo=timezone.utc), "keep": 7})
    assert "⚠️" not in t and "последняя в 14:32, новых 3" in t
    assert "💾 Бэкап: 06.10 04:00 · хранится 7" in t and "🔔 Оповещения о сбоях: выкл" in t
    bad = SimpleNamespace(poll_failures=3, last_poll_error="@c: <b>Boom</b>", last_poll_new=0, last_poll_at=None)
    t = main_text(rs, CHS, bad, {"last_at": None, "keep": 3})
    assert "⚠️ Сбоев опроса подряд: 3 — @c: &lt;b&gt;Boom&lt;/b&gt;" in t and "<b>Boom" not in t
    assert "последняя: ещё не было" in t and "💾 Бэкап: ещё не было · хранится 3" in t
    assert "💾 Бэкап: недоступен" in main_text(rs, CHS, None, None)


def test_settings_keyboard_buttons_and_back_targets():
    rs = rs_fake(alerts_enabled=True, log_retention_days=30, timezone="UTC", backup_keep=7)
    kb = settings_keyboard(rs)
    assert {"m:th", "m:md", "m:pi", "m:paid", "m:jr", "m:tz", "m:bk", "m:al", "m:main"} == set(datas(kb))
    assert "🔔 Оповещения: вкл" in texts(kb) and "🗑 Хранение журнала: 30 дн" in texts(kb)
    assert "💰 Платные: выкл" in texts(kb)
    assert "🔔 Оповещения: выкл" in texts(settings_keyboard(rs_fake(alerts_enabled=False)))
    assert "Пороги" in settings_text(rs) and "Бэкапы" in settings_text(rs)
    for kbd in (thresholds_keyboard(rs), interval_keyboard(rs), timezone_keyboard(rs), retention_keyboard(rs),
                backup_keyboard(rs, object())):
        assert datas(kbd)[-1] == "m:set"
    assert datas(back_keyboard("m:set")) == ["m:set"] and datas(back_keyboard()) == ["m:main"]
    assert all(len(d.encode()) < 64 for d in datas(kb))


async def test_settings_screen_and_paid_toggle_rerenders_settings():
    rs = rs_fake()
    menu = make_menu(rs, make_listener())
    cb = make_cb("m:set")
    await menu.on_callback(cb, FakeState())
    assert "<b>Настройки</b>" in cb.message.edit_text.await_args.args[0]
    cb = make_cb("m:paid")
    await menu.on_callback(cb, FakeState())
    rs.set.assert_awaited_with("show_paid_contact", True)
    assert "<b>Настройки</b>" in cb.message.edit_text.await_args.args[0]


async def test_alerts_toggle_live():
    rs = rs_fake(alerts_enabled=True)
    rs.set = AsyncMock(side_effect=lambda k, v: setattr(rs, k, v))
    menu = make_menu(rs, make_listener())
    cb = make_cb("m:al")
    await menu.on_callback(cb, FakeState())
    rs.set.assert_awaited_once_with("alerts_enabled", False)
    assert rs.alerts_enabled is False
    assert "🔔 Оповещения: выкл" in texts(cb.message.edit_text.await_args.kwargs["reply_markup"])


def make_backup_service(**kw):
    stamp = datetime(2026, 10, 6, 1, 0, tzinfo=timezone.utc)
    svc = MagicMock()
    svc.last_backup_at = None
    svc.last_error = None
    svc.keep = 7
    svc.list = MagicMock(return_value=[
        (SimpleNamespace(name="app-20261006-040000.db"), 2048, stamp),
        (SimpleNamespace(name="session-20261006-040000.session"), 100, stamp)])
    svc.backup_now = AsyncMock(return_value=SimpleNamespace(name="app-new.db"))
    for k, v in kw.items():
        setattr(svc, k, v)
    return svc


async def test_backup_screen_lists_db_only():
    rs = rs_fake(backup_keep=7)
    menu = make_menu(rs, make_listener())
    menu.backup_service = make_backup_service()
    cb = make_cb("m:bk")
    await menu.on_callback(cb, FakeState())
    text = cb.message.edit_text.await_args.args[0]
    assert "💾 <b>Бэкапы</b>" in text and "app-20261006-040000.db — 2 КБ, 06.10 04:00" in text
    assert "session-" not in text and "Хранить последних: 7" in text and "data/backups" in text
    kb = cb.message.edit_text.await_args.kwargs["reply_markup"]
    assert "✅ 7" in texts(kb) and {"bk:now", "bk:k:3", "bk:k:14", "bk:k:30", "m:set"} <= set(datas(kb))


async def test_backup_now_success_and_failure():
    rs = rs_fake(backup_keep=7)
    menu = make_menu(rs, make_listener())
    svc = menu.backup_service = make_backup_service()
    cb = make_cb("bk:now")
    await menu.on_callback(cb, FakeState())
    svc.backup_now.assert_awaited_once()
    cb.answer.assert_awaited()
    calls = cb.message.edit_text.await_args_list
    assert "⏳ Делаю бэкап…" in calls[0].args[0]
    assert "✅ Готово: app-new.db" in calls[-1].args[0]
    svc.backup_now = AsyncMock(side_effect=OSError("<disk> full " + "x" * 500))
    cb = make_cb("bk:now")
    await menu.on_callback(cb, FakeState())
    last = cb.message.edit_text.await_args_list[-1].args[0]
    assert "❌ Ошибка: OSError: &lt;disk&gt; full" in last and "<disk>" not in last
    assert len(last) < 1200


async def test_backup_without_service():
    menu = make_menu(rs_fake(), make_listener())
    cb = make_cb("m:bk")
    await menu.on_callback(cb, FakeState())
    text = cb.message.edit_text.await_args.args[0]
    kb = cb.message.edit_text.await_args.kwargs["reply_markup"]
    assert "только для SQLite" in text and datas(kb) == ["m:set"]
    cb = make_cb("bk:now")
    await menu.on_callback(cb, FakeState())
    assert cb.answer.await_args.kwargs.get("show_alert") is True


async def test_backup_keep_preset_sets_setting():
    rs = rs_fake(backup_keep=7)
    menu = make_menu(rs, make_listener())
    menu.backup_service = make_backup_service()
    await menu.on_callback(make_cb("bk:k:14"), FakeState())
    rs.set.assert_awaited_once_with("backup_keep", 14)
    rs.set = AsyncMock(side_effect=ValueError("плохо"))
    cb = make_cb("bk:k:99")
    await menu.on_callback(cb, FakeState())
    cb.answer.assert_awaited_with("плохо", show_alert=True)
    rs.set.reset_mock()
    await menu.on_callback(make_cb("bk:k:abc"), FakeState())
    rs.set.assert_not_awaited()


async def test_main_screen_uses_backup_service():
    rs = rs_fake(backup_keep=5)
    menu = make_menu(rs, make_listener())
    menu.backup_service = make_backup_service()
    cb = make_cb("m:main")
    await menu.on_callback(cb, FakeState())
    assert "💾 Бэкап: 06.10 04:00 · хранится 5" in cb.message.edit_text.await_args.args[0]


async def test_start_shows_welcome_with_commands_and_menu():
    menu = make_menu(rs_fake(), make_listener())
    msg = make_msg("/start")
    await menu.cmd_start(msg, FakeState())
    first = msg.answer.await_args_list[0].args[0]
    for cmd in ("/menu", "/channels", "/profile", "/stats", "/journal", "/help", "/cancel"):
        assert cmd in first
    assert len(WELCOME_TEXT) < 1500
    assert "🤖 <b>Поиск заказов</b>" in msg.answer.await_args_list[1].args[0]


def test_every_input_prompt_mentions_cancel():
    from src.bot import menu as m
    for text in (m.CHANNEL_PROMPT, m.UPLOAD_PROMPT, m.ADD_SKILLS_PROMPT, m.REMOVE_SKILLS_PROMPT,
                 m.INTERVAL_PROMPT, m.TIMEZONE_PROMPT, m.NEED_TEXT, m.PHOTO_REMINDER, m.UPLOAD_REMINDER,
                 m.model_prompt("a/b")):
        assert "/cancel — отмена" in text


async def test_check_now_from_main_rerenders_main():
    lst = make_listener()
    done = asyncio.Event()
    done.set()
    lst.request_poll_now = MagicMock(return_value=done)
    lst.last_poll_new = 2
    lst.last_poll_at = None
    lst.poll_failures = 0
    menu = make_menu(rs_fake(), lst)
    cb = make_cb("m:now")
    cb.message.answer = AsyncMock()
    await menu.on_callback(cb, FakeState())
    assert "<b>Поиск заказов</b>" in cb.message.edit_text.await_args.args[0]


def test_notifybot_backup_service_reaches_menu():
    from src.bot.bot import NotifyBot
    settings = SimpleNamespace(notify_bot_token="123456:ABCDEF", owner_telegram_id=42)
    nb = NotifyBot(settings, MagicMock(), None, {}, runtime_settings=rs_fake(), listener=make_listener())
    assert nb.handlers.menu.backup_service is None
    svc = make_backup_service()
    nb.backup_service = svc
    assert nb.handlers.menu.backup_service is svc and nb.backup_service is svc
