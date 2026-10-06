from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


from src import journal as jr
from src.bot.journal_view import (
    journal_data, journal_keyboard, journal_text, page_count, parse_journal_callback, retention_keyboard,
    retention_text, safe_url,
)
from src.bot.menu import MenuHandlers, MenuStates, main_keyboard, parse_menu_callback, settings_keyboard

COUNTS = {"all": 9, "sent": 2, "rules": 3, "jev": 1, "fit": 1, "paid": 0, "dup": 1, "pending": 1}


def entry(title="Python bot", chan="chan", url="https://t.me/chan/1", kind="sent", reason="отправлено, оценка 80",
          minute=5):
    return jr.JournalEntry(message_row_id=1, received_at=datetime(2026, 9, 30, 10, minute, tzinfo=timezone.utc),
                           channel_username=chan, url=url, title=title, kind=kind, reason=reason,
                           fit_score=80, route=None, contact_status=None)


def datas(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]


def texts(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def render(entries, kind="all", period="d", page=0, pages=1):
    return journal_text(entries, COUNTS, kind, period, page, pages, "Europe/Sofia")


def test_page_rendering_sample():
    t = render([entry(), entry(title="Дубль", kind="dup", reason="дубль вакансии #3", minute=4)])
    assert "📜 <b>Журнал</b> за 24 ч: всего 9 · ✅ отправлено 2 · 🧹 правила 3" in t
    assert "Фильтр: Все · период: 24 ч" in t
    assert '✅ 30.09 13:05 · @chan · <a href="https://t.me/chan/1">Python bot</a>' in t  # Sofia = UTC+3
    assert "   └ отправлено, оценка 80" in t
    assert "♻️" in t


def test_html_escaped_and_unsafe_link_dropped():
    t = render([entry(title="<script>alert(1)</script>", chan="a<b>", url="javascript:alert(1)",
                      reason="<i>x</i>")])
    assert "<script>" not in t and "&lt;script&gt;" in t
    assert "a<b>" not in t and "@a&lt;b&gt;" in t
    assert "<i>x</i>" not in t
    assert "<a " not in t


def test_url_quotes_escaped_and_safe_url():
    t = render([entry(url='https://x.com/"onmouseover="y')])
    assert '"onmouseover' not in t and "&quot;onmouseover" in t
    assert safe_url("t.me/chan/5") == "https://t.me/chan/5"
    assert safe_url("JAVASCRIPT:x") is None and safe_url("data:text/html,x") is None
    assert safe_url("https://a b") is None and safe_url(None) is None


def test_callback_parsing_and_validation():
    assert parse_journal_callback("j:sent:w:2") == ("sent", "w", 2)
    for bad in (None, "", "j:x:d:0", "j:all:z:0", "j:all:d:-1", "j:all:d:abc", "j:all:d", "j:all:d:0:1",
                "m:all:d:0", "j:all:d:99999999", "j:all:d:١"):
        assert parse_journal_callback(bad) is None
    assert journal_data("all", "d", -5) == "j:all:d:0"


def test_pagination_edges():
    kb = journal_keyboard("all", "d", 0, 1)
    assert "◀️" not in texts(kb) and "▶️" not in texts(kb)
    kb = journal_keyboard("all", "d", 0, 3)
    assert "▶️" in texts(kb) and "◀️" not in texts(kb) and "стр. 1" in texts(kb)
    kb = journal_keyboard("sent", "w", 2, 3)
    assert "◀️" in texts(kb) and "▶️" not in texts(kb)
    assert "j:sent:w:1" in datas(kb)
    assert page_count(0) == 1 and page_count(10) == 1 and page_count(11) == 2


def test_keyboard_marks_filter_period_and_lengths():
    kb = journal_keyboard("fit", "w", 0, 2)
    t = texts(kb)
    assert "• Не подошло по оценке" in t and "• 7 дн" in t and "24 ч" in t and "всё время" in t
    assert "🔄 Обновить" in t and "⬅️ Назад" in t
    assert all(len(d.encode()) < 64 for d in datas(kb))
    assert "j:jev:w:0" in datas(kb)


def test_length_under_limit_with_long_entries():
    long = "<>&" * 60
    es = [entry(title=long, chan="c" * 32, url="https://t.me/" + "a" * 300, reason=("<" * 150), minute=i)
          for i in range(10)]
    t = render(es)
    assert len(t) <= 4096
    t2 = render([entry(title="x" * 100, reason="y" * 200) for _ in range(10)])
    assert len(t2) <= 4096


def test_empty_state():
    t = render([], kind="paid")
    assert "Записей нет" in t and "Платный контакт" in t


def test_main_menu_has_journal_buttons():
    rs = SimpleNamespace(show_paid_contact=False, notifications_paused=False, log_retention_days=45)
    t = texts(main_keyboard(rs)) + texts(settings_keyboard(rs))
    assert "📜 Журнал" in t and "🗑 Хранение журнала: 45 дн" in t
    assert "j:all:d:0" in datas(main_keyboard(rs)) and "m:jr" in datas(settings_keyboard(rs))
    assert parse_menu_callback("jr:30") == ("jr", "30")


def rs_fake(days=30, dedup=14):
    async def _set(key, value):
        if value < min(max(7, dedup), 365) or value > 365:
            raise ValueError("Срок хранения журнала должен быть целым числом дней от 14 до 365")
        rs.log_retention_days = value
    rs = SimpleNamespace(log_retention_days=days, dedup_window_days=dedup, timezone="Europe/Sofia",
                         set=AsyncMock(side_effect=_set))
    return rs


def test_retention_presets_hidden_below_min():
    kb = retention_keyboard(rs_fake(dedup=14))
    assert "jr:14" in datas(kb) and "jr:365" in datas(kb)
    kb = retention_keyboard(rs_fake(days=90, dedup=60))
    d = datas(kb)
    assert "jr:14" not in d and "jr:30" not in d and "jr:60" in d and "jr:90" in d
    assert "✅ 90 дн" in texts(kb) and "jr:custom" in d
    assert "Минимум 60 дн" in retention_text(rs_fake(dedup=60))
    assert "хранятся всегда" in retention_text(rs_fake())


def make_cb(data):
    cb = MagicMock()
    cb.data = data
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    return cb


def make_msg(text=None):
    m = MagicMock()
    m.text = text
    m.answer = AsyncMock()
    return m


def make_state():
    st = MagicMock()
    st.set_state = AsyncMock()
    st.clear = AsyncMock()
    return st


def make_repo(entries=None):
    repo = MagicMock()
    repo.journal_counts = AsyncMock(return_value=dict(COUNTS))
    repo.journal = AsyncMock(return_value=entries if entries is not None else [entry()])
    return repo


async def test_journal_command_sends_one_message():
    repo = make_repo()
    h = MenuHandlers(repo, rs_fake())
    msg, st = make_msg("/journal"), make_state()
    await h.cmd_journal(msg, st)
    st.clear.assert_awaited()
    args, kw = msg.answer.call_args
    assert "📜 <b>Журнал</b> за 24 ч" in args[0] and kw["parse_mode"] == "HTML"
    repo.journal.assert_awaited_once()
    assert repo.journal.call_args.kwargs["kind"] == "all" and repo.journal.call_args.kwargs["limit"] == 10


async def test_journal_callback_edits_in_place_and_validates():
    repo = make_repo()
    h = MenuHandlers(repo, rs_fake())
    cb = make_cb("j:sent:a:0")
    await h.on_callback(cb, make_state())
    kw = repo.journal.call_args.kwargs
    assert kw["since"] is None and kw["kind"] == "sent"
    cb.message.edit_text.assert_awaited_once()
    cb.answer.assert_awaited()
    # invalid -> main journal
    repo.journal.reset_mock()
    cb2 = make_cb("j:evil:x:9")
    await h.on_callback(cb2, make_state())
    assert repo.journal.call_args.kwargs["kind"] == "all" and repo.journal.call_args.kwargs["since"] is not None


async def test_journal_page_clamped():
    repo = make_repo()
    h = MenuHandlers(repo, rs_fake())
    await h.on_callback(make_cb("j:all:d:999"), make_state())
    assert repo.journal.call_args.kwargs["offset"] == 0  # 9 messages -> single page


async def test_retention_preset_and_screen():
    rs = rs_fake()
    h = MenuHandlers(make_repo(), rs)
    cb = make_cb("m:jr")
    await h.on_callback(cb, make_state())
    assert "Хранение журнала</b>: 30 дн" in cb.message.edit_text.call_args.args[0]
    cb = make_cb("jr:90")
    await h.on_callback(cb, make_state())
    assert rs.log_retention_days == 90
    cb = make_cb("jr:abc")
    await h.on_callback(cb, make_state())
    assert rs.log_retention_days == 90


async def test_retention_custom_flow():
    rs = rs_fake()
    h = MenuHandlers(make_repo(), rs)
    st = make_state()
    await h.on_callback(make_cb("jr:custom"), st)
    st.set_state.assert_awaited_with(MenuStates.waiting_retention)
    # non-text -> reminder, stay
    msg = make_msg(None)
    await h.on_retention_input(msg, st)
    assert "число дней" in msg.answer.call_args.args[0]
    # invalid value -> error shown, stays in the state
    st2 = make_state()
    msg = make_msg("3")
    await h.on_retention_input(msg, st2)
    assert "от 14 до 365" in msg.answer.call_args.args[0]
    st2.clear.assert_not_awaited()
    msg = make_msg("abc")
    await h.on_retention_input(msg, st2)
    st2.clear.assert_not_awaited()
    # valid -> saved, state cleared
    msg = make_msg("45")
    await h.on_retention_input(msg, st2)
    assert rs.log_retention_days == 45
    st2.clear.assert_awaited()
    assert "Хранение журнала: 45 дн" in msg.answer.call_args_list[0].args[0]


def test_journal_command_registered():
    router = MagicMock()
    MenuHandlers(make_repo(), rs_fake()).register(router)
    names = [c.args[0].__name__ for c in router.message.register.call_args_list]
    assert "cmd_journal" in names and names.index("cmd_journal") < names.index("on_channel_input")
