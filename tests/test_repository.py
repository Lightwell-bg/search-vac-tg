from datetime import datetime, timedelta, timezone

from src.db.models import JobStatus


async def _msg(repo, mid=1, chan=100, text="hello"):
    return await repo.save_message(
        channel_tg_id=chan, channel_username="ch", message_id=mid,
        url=f"https://t.me/ch/{mid}", original_text=text, normalized_text=text,
    )


async def test_save_message_dedups_unique_pair(repo):
    id1, created1 = await _msg(repo)
    id2, created2 = await _msg(repo)
    assert created1 is True and created2 is False and id1 == id2
    assert await repo.message_exists(100, 1)
    assert not await repo.message_exists(100, 2)


async def test_last_message_id_only_increases(repo):
    await repo.upsert_channel(100, "ch", "Title")
    await repo.set_last_message_id(100, 10)
    await repo.set_last_message_id(100, 5)
    assert await repo.get_last_message_id(100) == 10
    assert await repo.get_last_message_id(999) == 0


async def test_create_job_and_duplicate(repo):
    m1, _ = await _msg(repo, 1)
    m2, _ = await _msg(repo, 2)
    job_id = await repo.create_job(m1, "h1", "text", "Title", "100$")
    assert await repo.find_job_by_hash("h1") == job_id
    assert await repo.find_job_by_hash("zz") is None
    await repo.link_duplicate(job_id, m2, "hash", 1.0)
    await repo.link_duplicate(job_id, m2, "hash", 1.0)  # idempotent
    sources = await repo.get_job_sources(job_id)
    assert [s.match_kind for s in sources] == ["original", "hash"]
    primary = await repo.get_primary_message(job_id)
    assert primary is not None and primary.id == m1
    stats = await repo.get_stats()
    assert stats["messages_received"] == 2 and stats["duplicates"] == 1


async def test_recent_job_keys(repo):
    m1, _ = await _msg(repo, 1)
    job_id = await repo.create_job(m1, "h", "some text", None, None, dedup_key="key1")
    since = datetime.now(timezone.utc) - timedelta(days=1)
    assert await repo.recent_job_keys(since, 10) == [(job_id, "key1")]


async def test_jobs_by_status_with_attempts(repo):
    ids = []
    for i in range(3):
        m, _ = await _msg(repo, i + 1)
        ids.append(await repo.create_job(m, f"h{i}", "t", None, None))
    await repo.update_job(ids[0], status=JobStatus.JEV_UNAVAILABLE)
    await repo.update_job(ids[1], status=JobStatus.JEV_UNAVAILABLE)
    await repo.update_job(ids[2], status=JobStatus.NEW)
    for _ in range(5):
        await repo.increment_attempts(ids[1])
    jobs = await repo.jobs_by_status([JobStatus.JEV_UNAVAILABLE], max_attempts=5)
    assert [j.id for j in jobs] == [ids[0]]
    assert await repo.increment_attempts(ids[0]) == 1


async def test_get_stats_counts(repo):
    m, _ = await _msg(repo)
    job_id = await repo.create_job(m, "h", "t", None, None)
    await repo.update_job(job_id, status=JobStatus.RULE_REJECTED, contact_status="paid_contact")
    await repo.log_jev_usage(job_id=job_id, decision="accept", cost_usd=0.5)
    await repo.log_jev_usage(job_id=job_id, decision="reject", error="boom", fallback_used=True, cost_usd=0.25)
    await repo.log_jev_usage(job_id=job_id, decision="review")
    await repo.log_llm_usage(job_id=job_id, purpose="job_review", model="m", cost_usd=0.1)
    await repo.log_llm_usage(job_id=job_id, purpose="application_generation", model="m", cost_usd=0.2)
    await repo.save_notification(job_id, 1, 2)
    await repo.save_notification(job_id, 1, 3, kind="application")
    await repo.save_feedback(job_id, "up", {"a": 1})
    await repo.save_feedback(job_id, "down", {})
    await repo.save_contacts(job_id, [{"kind": "username", "value": "@x", "source": "text"}])
    st = await repo.get_stats()
    assert st["rule_rejects"] == 1
    assert st["jev_processed"] == 3
    assert (st["jev_accepts"], st["jev_rejects"], st["jev_reviews"]) == (1, 1, 1)
    assert st["jev_errors"] == 1 and st["jev_fallbacks"] == 1
    assert st["openrouter_calls"] == 2 and st["openrouter_review_calls"] == 1
    assert st["notifications"] == 1
    assert (st["feedback_up"], st["feedback_down"]) == (1, 1)
    assert st["paid_contacts"] == 1
    assert abs(st["jev_cost_usd"] - 0.75) < 1e-9
    assert abs(st["openrouter_cost_usd"] - 0.3) < 1e-9
    assert st["jobs_by_status"] == {JobStatus.RULE_REJECTED: 1}


async def test_orphan_messages_filters_and_orders(repo):
    from datetime import datetime, timedelta, timezone

    def msg(mid, **kw):
        base = dict(channel_tg_id=1, channel_username="c", message_id=mid, url=None, posted_at=None,
                    original_text="t", normalized_text="text", buttons=[], extracted={})
        base.update(kw)
        return base

    a, _ = await repo.save_message(**msg(1))                       # orphan
    b, _ = await repo.save_message(**msg(2, normalized_text=""))   # empty text: not an orphan
    c, _ = await repo.save_message(**msg(3))
    await repo.create_job(c, "h", "text", None, None)              # has a job
    d, _ = await repo.save_message(**msg(4))
    await repo.link_duplicate(1, d, "hash", 100.0)                  # duplicate
    e, _ = await repo.save_message(**msg(5))                       # orphan, newer
    future = datetime.now(timezone.utc) + timedelta(minutes=1)
    assert [m.id for m in await repo.orphan_messages(future, 10)] == [a, e]
    assert [m.id for m in await repo.orphan_messages(future, 1)] == [a]
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    assert await repo.orphan_messages(past, 10) == []
