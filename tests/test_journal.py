"""Processing journal, retention cleanup, retention setting and local-time helpers."""
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src import journal as jr
from src.config import _valid_retention, _valid_timezone
from src.db.database import Database
from src.db.models import JobStatus
from src.settings_store import RuntimeSettings
from src.timeutil import fmt_local, to_local

NOW = datetime.now(timezone.utc)
LONG = "Нужен разработчик Telegram bot на aiogram, бюджет 50000. Пишите @x"
_n = 0


async def add(repo, *, text=LONG, status=None, age_days=0.0, title=None, dup_of=None, job=True, **job_fields):
    """Store a message (+ job unless job=False / dup_of). Returns (message_row_id, job_id)."""
    global _n
    _n += 1
    msg_id, _ = await repo.save_message(
        channel_tg_id=1, channel_username="chan", message_id=_n, url=f"https://t.me/chan/{_n}",
        posted_at=NOW, original_text=text, normalized_text=text, buttons=[], extracted={},
        received_at=NOW - timedelta(days=age_days))
    if dup_of is not None:
        await repo.link_duplicate(dup_of, msg_id, "hash", 100.0)
        return msg_id, dup_of
    if not job:
        return msg_id, None
    job_id = await repo.create_job(msg_id, f"h{_n}", text, title, None)
    fields = dict(job_fields)
    if status:
        fields["status"] = status
    # created_at follows the message age so retention sees an old job
    fields["created_at"] = NOW - timedelta(days=age_days)
    await repo.update_job(job_id, **fields)
    return msg_id, job_id


async def seed(repo):
    ids = {}
    ids["rule"] = await add(repo, status=JobStatus.RULE_REJECTED, age_days=0.6, route="rules",
                            decision_reason="negative_only: дизайн, логотип")
    ids["jev"] = await add(repo, status=JobStatus.JEV_REJECTED, age_days=0.5, route="JEV", fit_score=13,
                           decision_reason="JEV reject p=0.98",
                           jev_result={"decision": "reject", "raw_decision": "reject", "confidence": 0.98,
                                       "fit_raw": 0.4, "category": "non_tech"})
    ids["fit"] = await add(repo, status=JobStatus.FIT_REJECTED, age_days=0.4, route="JEV → OpenRouter review",
                           fit_score=57, decision_reason="не хватает опыта (score 57 < 65)",
                           llm_result={"reason": "не хватает опыта"})
    ids["sent"] = await add(repo, status=JobStatus.NOTIFIED, age_days=0.3, fit_score=88, title="Бот для CRM",
                            contact_status="direct")
    ids["paid"] = await add(repo, status=JobStatus.PAID_SKIPPED, age_days=0.2, contact_status="paid_contact")
    ids["dup"] = await add(repo, age_days=0.1, dup_of=ids["sent"][1])
    ids["pending"] = await add(repo, status=JobStatus.LLM_ERROR, age_days=0.05)
    ids["empty"] = await add(repo, text="", job=False, age_days=0.01)
    return ids


async def test_kinds_and_reasons(repo):
    ids = await seed(repo)
    entries = {e.message_row_id: e for e in await repo.journal(None, "all", 50, 0)}
    assert len(entries) == 8
    e = entries[ids["rule"][0]]
    assert e.kind == "rules" and e.reason == "нет технических слов, есть: дизайн, логотип"
    e = entries[ids["jev"][0]]
    assert e.kind == "jev"
    assert e.reason == "JEV: не подходит (уверенность 0.98, совпадение 0.4/3, нетехническое)"
    e = entries[ids["fit"][0]]
    assert e.kind == "fit" and e.reason == "оценка 57 < порога 65: не хватает опыта"
    assert e.fit_score == 57 and e.route == "JEV → OpenRouter review"
    e = entries[ids["sent"][0]]
    assert e.kind == "sent" and e.reason == "отправлено, оценка 88" and e.title == "Бот для CRM"
    assert e.contact_status == "direct"
    assert entries[ids["paid"][0]].kind == "paid" and entries[ids["paid"][0]].reason == "контакт платный"
    e = entries[ids["pending"][0]]
    assert e.kind == "pending" and "OpenRouter" in e.reason
    e = entries[ids["empty"][0]]
    assert e.kind == "rules" and e.reason == "нет текста"
    assert e.received_at.tzinfo is not None


async def test_duplicate_points_to_original(repo):
    ids = await seed(repo)
    sent_msg, sent_job = ids["sent"]
    e = next(x for x in await repo.journal(None, "dup", 10, 0))
    assert e.kind == "dup" and e.reason == f"дубль вакансии #{sent_job}"
    original = (await repo.get_primary_message(sent_job)).url
    assert e.url == original


async def test_filter_order_pagination_counts(repo):
    ids = await seed(repo)
    allr = await repo.journal(None, "all", 50, 0)
    assert [e.received_at for e in allr] == sorted((e.received_at for e in allr), reverse=True)
    assert allr[0].message_row_id == ids["empty"][0]
    page1 = await repo.journal(None, "all", 3, 0)
    page2 = await repo.journal(None, "all", 3, 3)
    page3 = await repo.journal(None, "all", 3, 6)
    got = [e.message_row_id for e in page1 + page2 + page3]
    assert got == [e.message_row_id for e in allr] and len(page3) == 2
    assert {e.kind for e in await repo.journal(None, "rules", 50, 0)} == {"rules"}
    assert len(await repo.journal(None, "rules", 50, 0)) == 2
    counts = await repo.journal_counts(None)
    assert counts == {"all": 8, "sent": 1, "rules": 2, "jev": 1, "fit": 1, "paid": 1, "dup": 1, "pending": 1}
    since = NOW - timedelta(hours=6)  # only messages newer than 0.25 days
    recent = await repo.journal(since, "all", 50, 0)
    assert len(recent) == 4
    assert (await repo.journal_counts(since))["all"] == 4
    with pytest.raises(ValueError):
        await repo.journal(None, "bogus", 5, 0)


async def test_title_fallback_and_limit(repo):
    long = "A" * 300 + "\nвторая строка"
    msg, _ = await add(repo, text=long, status=JobStatus.RULE_REJECTED)
    e = next(x for x in await repo.journal(None, "all", 5, 0) if x.message_row_id == msg)
    assert len(e.title) <= 100


@pytest.mark.parametrize("raw,expected", [
    ("too_short", "слишком короткий текст"),
    ("no_job_signal", "не похоже на вакансию"),
    ("strong_negative_only: казино", "нет технических слов, есть: казино"),
])
def test_rule_reason_mapping(raw, expected):
    e = jr.JournalEntry(1, NOW, "c", None, "t", "rules", "", None, "rules", None, job_id=5,
                        status="rule_rejected", decision_reason=raw)
    assert jr.describe(e) == expected


def test_describe_falls_back_to_decision_reason():
    e = jr.JournalEntry(1, NOW, "c", None, "t", "rules", "", None, None, None, job_id=5,
                        decision_reason="что-то новое")
    assert jr.describe(e) == "что-то новое"


# ------------------------------------------------------------------ cleanup

async def _ids_left(repo):
    return ({e.message_row_id for e in await repo.journal(None, "all", 500, 0)},
            await repo.get_stats())


async def test_cleanup_keeps_protected_and_deletes_old(repo):
    old = 60
    m_rule, j_rule = await add(repo, status=JobStatus.RULE_REJECTED, age_days=old)
    m_fit, j_fit = await add(repo, status=JobStatus.FIT_REJECTED, age_days=old)
    m_sent, j_sent = await add(repo, status=JobStatus.NOTIFIED, age_days=old)
    await repo.save_notification(j_sent, 1, 10)
    await repo.save_contacts(j_sent, [{"kind": "username", "value": "@a", "source": "text"}])
    m_dup, _ = await add(repo, age_days=old - 1, dup_of=j_sent)       # old duplicate of a kept job
    m_fb, j_fb = await add(repo, status=JobStatus.FIT_REJECTED, age_days=old)
    await repo.save_feedback(j_fb, "down", {})
    m_retry, j_retry = await add(repo, status=JobStatus.JEV_UNAVAILABLE, age_days=old)
    m_prog, j_prog = await add(repo, status=JobStatus.RULE_REJECTED, age_days=old)
    m_empty, _ = await add(repo, text="", job=False, age_days=old)
    m_new, j_new = await add(repo, status=JobStatus.RULE_REJECTED, age_days=1)
    m_dup_new, _ = await add(repo, age_days=0, dup_of=j_fit)          # fresh repost keeps the old job alive
    await repo.log_jev_usage(job_id=j_rule, purpose="job_classification", decision="reject")
    await repo.log_llm_usage(job_id=j_rule, purpose="job_review", model="m")

    res = await jr.cleanup_old(repo, 30, in_progress={j_prog})
    left, stats = await _ids_left(repo)
    assert m_rule not in left and m_empty not in left
    assert {m_sent, m_fb, m_retry, m_prog, m_new, m_fit, m_dup_new, m_dup} <= left
    assert res["jobs"] == 1 and res["messages"] == 2     # j_rule + its message, the empty one
    assert res["jev_usage"] == 1 and res["llm_usage"] == 1
    assert await repo.get_job(j_rule) is None
    for j in (j_sent, j_fb, j_retry, j_prog, j_fit, j_new):
        assert await repo.get_job(j) is not None
    assert len(await repo.get_job_sources(j_sent)) == 2       # primary and duplicate sources of a kept job stay
    assert stats["notifications"] == 1 and stats["feedback_down"] == 1
    assert stats["since"] is not None and stats["since"].tzinfo is not None


async def test_cleanup_second_run_deletes_nothing_more(repo):
    await add(repo, status=JobStatus.RULE_REJECTED, age_days=90)
    first = await jr.cleanup_old(repo, 30)
    second = await jr.cleanup_old(repo, 30)
    assert first["jobs"] == 1 and first["messages"] == 1
    assert second["jobs"] == 0 and second["messages"] == 0


async def test_cleanup_keeps_channels_and_settings(repo):
    await repo.upsert_channel(1, "chan", "Chan")
    await repo.set_setting("notify_score", 70)
    await add(repo, status=JobStatus.RULE_REJECTED, age_days=90)
    await jr.cleanup_old(repo, 30)
    assert len(await repo.list_channels()) == 1
    assert (await repo.get_settings())["notify_score"] == 70


async def test_cleanup_logs(repo, caplog):
    await add(repo, status=JobStatus.RULE_REJECTED, age_days=90)
    with caplog.at_level(logging.INFO, logger="journal"):
        await jr.cleanup_old(repo, 30)
    assert "JOURNAL_CLEANUP deleted 1 messages, 1 jobs" in caplog.text


async def test_received_at_index_created_on_old_db(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'o.db').as_posix()}")
    await db.init()
    async with db.engine.begin() as conn:
        await conn.exec_driver_sql("DROP INDEX ix_messages_received_at")
    await db.init()
    async with db.engine.begin() as conn:
        rows = (await conn.exec_driver_sql("PRAGMA index_list(messages)")).all()
    assert any(r[1] == "ix_messages_received_at" for r in rows)
    await db.close()


# ------------------------------------------------------------------ retention setting

def env(**kw):
    base = dict(notify_score=65, high_fit_score=80, show_paid_contact=False, openrouter_model="a/b",
                poll_interval_sec=120, log_retention_days=30, dedup_window_days=14)
    base.update(kw)
    return SimpleNamespace(**base)


async def test_retention_default_and_persist(repo):
    rs = await RuntimeSettings.load(repo, env(log_retention_days=45))
    assert rs.log_retention_days == 45
    await rs.set("log_retention_days", 90)
    assert (await RuntimeSettings.load(repo, env())).log_retention_days == 90
    await rs.set("log_retention_days", "120")
    assert rs.log_retention_days == 120


@pytest.mark.parametrize("value", [13, 6, 366, 0, -5, "abc", True, 30.5])
async def test_retention_validation_with_dedup_window(repo, value):
    rs = await RuntimeSettings.load(repo, env())   # dedup window 14 -> minimum 14
    with pytest.raises(ValueError, match="14"):
        await rs.set("log_retention_days", value)
    assert rs.log_retention_days == 30


async def test_retention_minimum_is_seven_without_big_dedup_window(repo):
    rs = await RuntimeSettings.load(repo, env(dedup_window_days=3))
    await rs.set("log_retention_days", 7)
    with pytest.raises(ValueError):
        await rs.set("log_retention_days", 6)


async def test_stored_retention_below_dedup_window_ignored(repo):
    await repo.set_setting("log_retention_days", 10)
    assert (await RuntimeSettings.load(repo, env())).log_retention_days == 30


def test_config_retention_clamp():
    assert _valid_retention(30, 14) == 30
    assert _valid_retention(3, 14) == 14
    assert _valid_retention(999, 14) == 365
    assert _valid_retention(5, 3) == 7


# ------------------------------------------------------------------ timezone

def test_fmt_local_sofia_winter_and_summer():
    assert fmt_local(datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc), "Europe/Sofia") == "15.01 12:00"
    assert fmt_local(datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc), "Europe/Sofia") == "15.07 13:00"


def test_fmt_local_dst_boundaries():
    # Sofia: DST starts 2026-03-29 01:00 UTC, ends 2026-10-25 01:00 UTC
    f = "%d.%m %H:%M"
    assert fmt_local(datetime(2026, 3, 29, 0, 59, tzinfo=timezone.utc), "Europe/Sofia", f) == "29.03 02:59"
    assert fmt_local(datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc), "Europe/Sofia", f) == "29.03 04:00"
    assert fmt_local(datetime(2026, 10, 25, 0, 59, tzinfo=timezone.utc), "Europe/Sofia", f) == "25.10 03:59"
    assert fmt_local(datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc), "Europe/Sofia", f) == "25.10 03:00"


def test_naive_datetime_is_utc_and_invalid_tz_falls_back():
    naive = datetime(2026, 7, 15, 10, 0)
    assert to_local(naive, "Europe/Sofia").hour == 13
    assert fmt_local(naive, "Not/AZone", "%H:%M") == "10:00"
    assert _valid_timezone("Not/AZone") == "UTC"
    assert _valid_timezone("Europe/Sofia") == "Europe/Sofia"
