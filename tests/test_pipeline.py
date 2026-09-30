from datetime import timedelta

import pytest
from sqlalchemy import select

from src.db.models import Contact, JevUsage, JobStatus, LlmUsage, Notification
from src.filtering.pipeline import (
    ROUTE_JEV,
    ROUTE_JEV_FALLBACK,
    ROUTE_JEV_REVIEW,
    ROUTE_RULES,
    PipelineOptions,
)
from src.jev.schemas import JevError
from src.llm.schemas import LlmError
from src.telegram.contact_resolver import CallbackAnswer
from src.telegram.parser import ButtonInfo

from .helpers import (
    AMBIGUOUS_TEXT,
    DESIGN_TEXT,
    SMM_TEXT,
    TECH_TEXT,
    FakeActions,
    FakeJevClient,
    FakeLLM,
    FakeNotifier,
    build_pipeline,
    jev_answers,
    make_post,
    review,
)

NO_CONTACT_TEXT = "Нужен разработчик Telegram bot на aiogram: приём заявок и интеграция с CRM. Бюджет: 50 000 руб."


def bad_answers():
    a = jev_answers()
    a["decision"]["choice"] = "maybe"
    return a


JEV_RESULTS = {
    "ACCEPT": jev_answers("accept", 0.95, 3.0),
    "REJECT": jev_answers("reject", 0.95, 0.0, "non_tech"),
    "REVIEW": jev_answers("review", 0.6, 1.5),
    "ERROR": JevError("http", "HTTP 500"),
    "TIMEOUT": JevError("timeout", "timed out"),
    "INVALID_RESULT": bad_answers(),
}


async def rows(repo, model):
    async with repo.db.session() as s:
        return list((await s.execute(select(model).order_by(model.id))).scalars().all())


# ------------------------------------------------------------------ JEV routing matrix


@pytest.mark.parametrize("name,outcome,status,llm_calls", [
    ("ACCEPT", "notified", JobStatus.NOTIFIED, 0),
    ("REJECT", "jev_rejected", JobStatus.JEV_REJECTED, 0),
    ("REVIEW", "notified", JobStatus.NOTIFIED, 1),
    ("ERROR", "notified", JobStatus.NOTIFIED, 1),
    ("TIMEOUT", "notified", JobStatus.NOTIFIED, 1),
    ("INVALID_RESULT", "notified", JobStatus.NOTIFIED, 1),
])
async def test_jev_routing_with_fallback(repo, name, outcome, status, llm_calls):
    pipe, jev, llm, actions, notifier = build_pipeline(repo, jev=FakeJevClient(JEV_RESULTS[name]))
    assert await pipe.process_post(make_post(TECH_TEXT)) == outcome
    job = await repo.get_job(1)
    assert job.status == status
    assert llm.calls == llm_calls
    assert len(notifier.calls) == (1 if outcome == "notified" else 0)
    usage = (await rows(repo, JevUsage))[0]
    if name in ("ERROR", "TIMEOUT", "INVALID_RESULT"):
        assert usage.error and usage.fallback_used is True and usage.decision is None
        assert job.route == ROUTE_JEV_FALLBACK == "JEV error → OpenRouter"
        assert llm.args[0][2] == "unavailable (error)"
    else:
        assert usage.error is None and usage.fallback_used is False
        assert usage.decision == JEV_RESULTS[name]["decision"]["choice"]


async def test_jev_accept_route_and_no_llm(repo):
    pipe, jev, llm, *_ = build_pipeline(repo, jev=FakeJevClient(JEV_RESULTS["ACCEPT"]))
    await pipe.process_post(make_post(TECH_TEXT))
    job = await repo.get_job(1)
    assert job.route == ROUTE_JEV and job.category == "telegram_automation"
    assert job.fit_score == 100 and llm.calls == 0 and job.llm_result is None
    assert len(await rows(repo, LlmUsage)) == 0


async def test_jev_review_route_and_llm_args(repo):
    pipe, jev, llm, *_ = build_pipeline(repo, jev=FakeJevClient(JEV_RESULTS["REVIEW"]))
    await pipe.process_post(make_post(TECH_TEXT))
    job = await repo.get_job(1)
    assert job.route == ROUTE_JEV_REVIEW == "JEV → OpenRouter review"
    assert "decision=review" in llm.args[0][2]
    assert len(await rows(repo, LlmUsage)) == 1


@pytest.mark.parametrize("name", ["ERROR", "TIMEOUT", "INVALID_RESULT"])
async def test_jev_error_without_fallback_keeps_vacancy(repo, name):
    pipe, jev, llm, actions, notifier = build_pipeline(
        repo, jev=FakeJevClient(JEV_RESULTS[name]), options=PipelineOptions(jev_fallback_to_openrouter=False))
    assert await pipe.process_post(make_post(TECH_TEXT)) == "jev_unavailable"
    job = await repo.get_job(1)
    assert job.status == JobStatus.JEV_UNAVAILABLE and job.attempts == 1 and job.last_error
    assert llm.calls == 0 and notifier.calls == []
    usage = (await rows(repo, JevUsage))[0]
    assert usage.error and usage.fallback_used is False


# ------------------------------------------------------------------ the four cases


async def test_case1_designer_rule_rejected(repo):
    pipe, jev, llm, _, notifier = build_pipeline(repo)
    assert await pipe.process_post(make_post(DESIGN_TEXT)) == "rule_rejected"
    job = await repo.get_job(1)
    assert job.status == JobStatus.RULE_REJECTED and job.route == ROUTE_RULES
    assert jev.calls == [] and llm.calls == 0 and notifier.calls == []


async def test_case2_technical_accept_notified(repo):
    pipe, jev, llm, actions, notifier = build_pipeline(repo, jev=FakeJevClient(JEV_RESULTS["ACCEPT"]))
    assert await pipe.process_post(make_post(TECH_TEXT)) == "notified"
    job = await repo.get_job(1)
    assert job.status == JobStatus.NOTIFIED
    assert job.contact_status == "direct" and job.contact_value == "@client_user"
    assert job.budget and "50 000" in job.budget
    assert llm.calls == 0 and actions.clicks == 0
    assert notifier.calls == [1]
    notes = await rows(repo, Notification)
    assert len(notes) == 1 and notes[0].job_id == 1 and notes[0].chat_id == 1
    assert notes[0].tg_message_id == 100 and notes[0].kind == "job"
    contacts = await rows(repo, Contact)
    assert [(c.kind, c.value) for c in contacts] == [("username", "@client_user")]
    # only profile + job (+ keywords) go to JEV
    state = jev.calls[0][0]
    assert set(state) <= {"profile", "job", "keywords_found"}


async def test_case3_ambiguous_review_llm_accept(repo):
    llm = FakeLLM(review(85, True))
    pipe, jev, llm, _, notifier = build_pipeline(repo, jev=FakeJevClient(JEV_RESULTS["REVIEW"]), llm=llm)
    assert await pipe.process_post(make_post(AMBIGUOUS_TEXT)) == "notified"
    job = await repo.get_job(1)
    assert llm.calls == 1 and job.route == "JEV → OpenRouter review"
    assert job.status == JobStatus.NOTIFIED and job.llm_result["fit_score"] == 85
    assert job.fit_score >= 65
    assert notifier.calls == [1]


async def test_case3_ambiguous_review_llm_says_no(repo):
    llm = FakeLLM(review(90, False, reason="not a fit"))
    pipe, jev, llm, _, notifier = build_pipeline(repo, jev=FakeJevClient(JEV_RESULTS["REVIEW"]), llm=llm)
    assert await pipe.process_post(make_post(AMBIGUOUS_TEXT)) == "fit_rejected"
    job = await repo.get_job(1)
    assert job.status == JobStatus.FIT_REJECTED and notifier.calls == []
    assert llm.calls == 1


async def test_case3_llm_low_score_fit_rejected(repo):
    llm = FakeLLM(review(20, True))
    pipe, *_rest = build_pipeline(repo, jev=FakeJevClient(JEV_RESULTS["REVIEW"]), llm=llm)
    assert await pipe.process_post(make_post(AMBIGUOUS_TEXT)) == "fit_rejected"


async def test_case4_jev_reject_stops(repo):
    pipe, jev, llm, actions, notifier = build_pipeline(repo, jev=FakeJevClient(JEV_RESULTS["REJECT"]))
    assert await pipe.process_post(make_post(SMM_TEXT + " Ещё нужен telegram bot")) == "jev_rejected"
    job = await repo.get_job(1)
    assert job.status == JobStatus.JEV_REJECTED and job.route == ROUTE_JEV
    assert llm.calls == 0 and notifier.calls == [] and actions.clicks == 0
    assert job.contact_status is None


async def test_low_confidence_accept_goes_to_llm(repo):
    pipe, jev, llm, *_ = build_pipeline(repo, jev=FakeJevClient(jev_answers("accept", 0.5, 3.0)))
    await pipe.process_post(make_post(TECH_TEXT))
    assert llm.calls == 1


# ------------------------------------------------------------------ paid contact


PAID_BUTTONS = [ButtonInfo("Получить контакт за 50 ⭐", "callback", data_hex="00")]


async def test_paid_contact_skipped_by_default(repo):
    pipe, jev, llm, actions, notifier = build_pipeline(repo, jev=FakeJevClient(JEV_RESULTS["ACCEPT"]))
    assert await pipe.process_post(make_post(NO_CONTACT_TEXT, buttons=PAID_BUTTONS)) == "paid_skipped"
    job = await repo.get_job(1)
    assert job.status == JobStatus.PAID_SKIPPED and job.contact_status == "paid_contact"
    assert notifier.calls == [] and actions.clicks == 0


async def test_paid_contact_shown_when_enabled(repo):
    pipe, jev, llm, actions, notifier = build_pipeline(
        repo, jev=FakeJevClient(JEV_RESULTS["ACCEPT"]), options=PipelineOptions(show_paid_contact=True))
    assert await pipe.process_post(make_post(NO_CONTACT_TEXT, buttons=PAID_BUTTONS)) == "notified"
    job = await repo.get_job(1)
    assert job.status == JobStatus.NOTIFIED and job.contact_status == "paid_contact"
    assert notifier.calls == [1] and actions.clicks == 0


async def test_paid_skipped_is_not_retried(repo):
    pipe, *_ = build_pipeline(repo, jev=FakeJevClient(JEV_RESULTS["ACCEPT"]))
    await pipe.process_post(make_post(NO_CONTACT_TEXT, buttons=PAID_BUTTONS))
    assert await pipe.retry_pending() == 0


async def test_free_contact_via_callback_in_pipeline(repo):
    actions = FakeActions(CallbackAnswer("Контакт: @client_user"))
    pipe, jev, llm, actions, notifier = build_pipeline(
        repo, jev=FakeJevClient(JEV_RESULTS["ACCEPT"]), actions=actions)
    buttons = [ButtonInfo("Получить контакт", "callback", data_hex="00")]
    assert await pipe.process_post(make_post(NO_CONTACT_TEXT, buttons=buttons)) == "notified"
    job = await repo.get_job(1)
    assert job.contact_status == "free" and job.contact_value == "@client_user" and actions.clicks == 1


# ------------------------------------------------------------------ retries


async def test_llm_error_then_retry_succeeds(repo):
    llm = FakeLLM(LlmError("timeout", "slow"))
    pipe, jev, llm, actions, notifier = build_pipeline(
        repo, jev=FakeJevClient(JEV_RESULTS["REVIEW"]), llm=llm)
    assert await pipe.process_post(make_post(TECH_TEXT)) == "llm_error"
    job = await repo.get_job(1)
    assert job.status == JobStatus.LLM_ERROR and job.attempts == 1 and "timeout" in job.last_error
    usage = await rows(repo, LlmUsage)
    assert len(usage) == 1 and usage[0].error
    assert notifier.calls == []

    llm.behavior = review(85, True)
    assert await pipe.retry_pending() == 1
    job = await repo.get_job(1)
    assert job.status == JobStatus.NOTIFIED and llm.calls == 2
    assert len(jev.calls) == 1  # JEV result reused, not asked again
    assert notifier.calls == [1]
    assert job.route == ROUTE_JEV_REVIEW


async def test_llm_missing_marks_llm_error(repo):
    pipe, jev, llm, _, notifier = build_pipeline(repo, jev=FakeJevClient(JEV_RESULTS["REVIEW"]), llm_none=True)
    assert await pipe.process_post(make_post(TECH_TEXT)) == "llm_error"
    assert (await repo.get_job(1)).status == JobStatus.LLM_ERROR


async def test_jev_unavailable_then_retry_processed(repo):
    pipe, jev, llm, actions, notifier = build_pipeline(
        repo, jev=FakeJevClient(JEV_RESULTS["TIMEOUT"]), options=PipelineOptions(jev_fallback_to_openrouter=False))
    assert await pipe.process_post(make_post(TECH_TEXT)) == "jev_unavailable"
    jev.behavior = JEV_RESULTS["ACCEPT"]
    assert await pipe.retry_pending() == 1
    job = await repo.get_job(1)
    assert job.status == JobStatus.NOTIFIED and llm.calls == 0
    assert len(jev.calls) == 2 and notifier.calls == [1]


async def test_retry_respects_attempt_limit(repo):
    pipe, jev, *_ = build_pipeline(
        repo, jev=FakeJevClient(JEV_RESULTS["ERROR"]),
        options=PipelineOptions(jev_fallback_to_openrouter=False, retry_limit=2))
    await pipe.process_post(make_post(TECH_TEXT))
    await pipe.retry_pending()
    await pipe.retry_pending()
    calls = len(jev.calls)
    assert calls == 2  # attempts hit the limit of 2 -> no third call
    await pipe.retry_pending()
    assert len(jev.calls) == calls


async def test_notify_error_then_retry_without_reresolving_contact(repo):
    actions = FakeActions(CallbackAnswer("Контакт: @client_user"))
    notifier = FakeNotifier(error=ConnectionError("telegram down"))
    pipe, jev, llm, actions, notifier = build_pipeline(
        repo, jev=FakeJevClient(JEV_RESULTS["ACCEPT"]), actions=actions, notifier=notifier)
    buttons = [ButtonInfo("Получить контакт", "callback", data_hex="00")]
    assert await pipe.process_post(make_post(NO_CONTACT_TEXT, buttons=buttons)) == "notify_error"
    job = await repo.get_job(1)
    assert job.status == JobStatus.NOTIFY_ERROR and job.attempts == 1
    assert job.contact_status == "free" and actions.clicks == 1

    notifier.error = None
    assert await pipe.retry_pending() == 1
    job = await repo.get_job(1)
    assert job.status == JobStatus.NOTIFIED
    assert actions.clicks == 1  # contact was not re-resolved
    assert len(jev.calls) == 1
    assert len(await rows(repo, Notification)) == 1
    assert len(notifier.calls) == 2


async def test_no_notifier_marks_notify_error(repo):
    pipe, *_ = build_pipeline(repo, jev=FakeJevClient(JEV_RESULTS["ACCEPT"]))
    pipe.notifier = None
    assert await pipe.process_post(make_post(TECH_TEXT)) == "notify_error"
    assert (await repo.get_job(1)).status == JobStatus.NOTIFY_ERROR


# ------------------------------------------------------------------ robustness


async def test_same_message_twice_is_seen(repo):
    pipe, jev, *_ = build_pipeline(repo)
    assert await pipe.process_post(make_post(TECH_TEXT, 7)) == "notified"
    assert await pipe.process_post(make_post(TECH_TEXT, 7)) == "seen"
    assert len(jev.calls) == 1


async def test_process_post_never_raises(repo):
    # a plain RuntimeError from the JEV adapter is a JEV error -> OpenRouter fallback
    pipe, jev, *_ = build_pipeline(repo, jev=FakeJevClient(RuntimeError("boom")))
    assert await pipe.process_post(make_post(TECH_TEXT)) == "notified"


async def test_empty_text_outcome(repo):
    pipe, jev, *_ = build_pipeline(repo)
    assert await pipe.process_post(make_post("   ━━━━━━━   ")) == "empty"
    assert jev.calls == []


async def test_short_text_rule_rejected(repo):
    pipe, jev, *_ = build_pipeline(repo)
    assert await pipe.process_post(make_post("Нужен бот")) == "rule_rejected"
    assert jev.calls == []


async def test_resolver_exception_becomes_contact_unknown(repo):
    pipe, *_ = build_pipeline(repo, jev=FakeJevClient(JEV_RESULTS["ACCEPT"]))

    class Boom:
        async def resolve(self, post, contacts):
            raise RuntimeError("x")
    pipe.resolver = Boom()
    assert await pipe.process_post(make_post(TECH_TEXT)) == "notified"
    assert (await repo.get_job(1)).contact_status == "contact_unknown"


# ------------------------------------------------------------------ stats


async def test_stats_after_mixed_run(repo):
    queue = [
        JEV_RESULTS["ACCEPT"],            # post B
        JEV_RESULTS["REVIEW"],            # post C
        JEV_RESULTS["REJECT"],            # post D
        JEV_RESULTS["ERROR"],             # post E (fallback -> LLM)
    ]
    jev = FakeJevClient(lambda state: queue.pop(0))
    pipe, jev, llm, actions, notifier = build_pipeline(repo, jev=jev, llm=FakeLLM(review(85, True)))
    posts = [
        make_post(DESIGN_TEXT, 1),                                       # A rule reject
        make_post(TECH_TEXT, 2),                                         # B JEV accept -> notified
        make_post(AMBIGUOUS_TEXT, 3),                                    # C JEV review -> LLM -> notified
        make_post("Требуется юрист и n8n автоматизация договоров для нашей фирмы. Пишите @client_three", 4),  # D reject
        make_post("Ищем Python разработчика для парсинга маркетплейсов и выгрузки цен в Google Sheets, "
                  "нужен опыт scraping и docker. Пишите @client_two", 5),   # E jev error -> LLM -> notified
        make_post(TECH_TEXT, 6, 200, "chan_two"),                        # F duplicate of B
    ]
    outcomes = [await pipe.process_post(p) for p in posts]
    assert outcomes == ["rule_rejected", "notified", "notified", "jev_rejected", "notified", "duplicate"]

    st = await repo.get_stats()
    assert st["messages_received"] == 6
    assert st["duplicates"] == 1
    assert st["rule_rejects"] == 1
    assert st["jev_processed"] == 4
    assert st["jev_accepts"] == 1 and st["jev_rejects"] == 1 and st["jev_reviews"] == 1
    assert st["jev_errors"] == 1 and st["jev_fallbacks"] == 1
    assert st["openrouter_review_calls"] == 2 and st["openrouter_calls"] == 2
    assert st["notifications"] == 3
    assert llm.calls == 2 and len(notifier.calls) == 3
    assert st["jobs_by_status"] == {"rule_rejected": 1, "notified": 3, "jev_rejected": 1}
    assert st["jev_cost_usd"] == pytest.approx(0.0003)
    assert st["openrouter_cost_usd"] == pytest.approx(0.002)


# ------------------------------------------------------------------ durability (Codex review)


def _fail_once(monkeypatch, obj, name):
    """Make ``obj.name`` raise on the first call and behave normally afterwards."""
    original = getattr(obj, name)
    state = {"raised": False}

    async def flaky(*a, **k):
        if not state["raised"]:
            state["raised"] = True
            raise RuntimeError(f"{name} boom")
        return await original(*a, **k)

    monkeypatch.setattr(obj, name, flaky)


async def _job_count(repo):
    from sqlalchemy import func

    from src.db.models import Job
    async with repo.db.session() as s:
        return (await s.execute(select(func.count()).select_from(Job))).scalar_one()


async def test_orphan_message_recovered_by_retry_pending(repo, monkeypatch):
    import src.filtering.pipeline as pl
    pipe, jev, llm, actions, notifier = build_pipeline(repo)
    _fail_once(monkeypatch, repo, "create_job")
    assert await pipe.process_post(make_post(TECH_TEXT)) == "failed"
    assert await _job_count(repo) == 0 and notifier.calls == []
    monkeypatch.setattr(pl, "ORPHAN_GRACE", timedelta(0))
    assert await pipe.retry_pending() >= 1
    job = await repo.get_job(1)
    assert job is not None and job.status == JobStatus.NOTIFIED and notifier.calls == [1]
    assert await pipe.process_post(make_post(TECH_TEXT)) == "seen"


async def test_orphan_not_touched_within_grace_period(repo, monkeypatch):
    pipe, jev, llm, actions, notifier = build_pipeline(repo)
    _fail_once(monkeypatch, repo, "create_job")
    assert await pipe.process_post(make_post(TECH_TEXT)) == "failed"
    assert await pipe.retry_pending() == 0  # default grace: the task may still be running
    assert await _job_count(repo) == 0


async def test_stage_exception_keeps_job_new_and_retry_completes(repo, monkeypatch):
    import src.filtering.pipeline as pl
    pipe, jev, llm, actions, notifier = build_pipeline(repo)
    original = pl.decide
    state = {"raised": False}

    def flaky(*a, **k):
        if not state["raised"]:
            state["raised"] = True
            raise RuntimeError("scorer boom")
        return original(*a, **k)

    monkeypatch.setattr(pl, "decide", flaky)
    assert await pipe.process_post(make_post(TECH_TEXT)) == "failed"
    job = await repo.get_job(1)
    assert job.status == JobStatus.NEW and job.attempts == 1 and "scorer boom" in (job.last_error or "")
    monkeypatch.setattr(pl, "ORPHAN_GRACE", timedelta(0))
    assert await pipe.retry_pending() == 1
    assert (await repo.get_job(1)).status == JobStatus.NOTIFIED and notifier.calls == [1]


async def test_contact_claim_prevents_second_click(repo, monkeypatch):
    actions = FakeActions(CallbackAnswer("Контакт: @client_user"))
    pipe, jev, llm, actions, notifier = build_pipeline(
        repo, jev=FakeJevClient(JEV_RESULTS["ACCEPT"]), actions=actions)
    _fail_once(monkeypatch, repo, "save_contacts")
    buttons = [ButtonInfo("Получить контакт", "callback", data_hex="00")]
    assert await pipe.process_post(make_post(NO_CONTACT_TEXT, buttons=buttons)) == "failed"
    job = await repo.get_job(1)
    assert job.contact_status == "resolving" and actions.clicks == 1
    assert await pipe.retry_pending() == 1
    job = await repo.get_job(1)
    assert actions.clicks == 1  # never pressed twice
    assert job.status == JobStatus.NOTIFIED and job.contact_status != "resolving"
    assert notifier.calls == [1]


async def test_notifying_claim_becomes_uncertain_and_is_not_resent(repo):
    pipe, jev, llm, actions, notifier = build_pipeline(repo)
    assert await pipe.process_post(make_post(TECH_TEXT)) == "notified"
    await repo.update_job(1, status=JobStatus.NOTIFYING)  # simulate a crash mid-send
    assert await pipe.retry_pending() == 1
    job = await repo.get_job(1)
    assert job.status == JobStatus.NOTIFY_UNCERTAIN
    assert notifier.calls == [1]  # not called again
    assert await pipe.retry_pending() == 0  # uncertain is final, not retried


async def test_save_notification_failure_after_send_still_notified(repo, monkeypatch):
    pipe, jev, llm, actions, notifier = build_pipeline(repo)

    async def boom(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(repo, "save_notification", boom)
    assert await pipe.process_post(make_post(TECH_TEXT)) == "notified"
    assert (await repo.get_job(1)).status == JobStatus.NOTIFIED
    assert notifier.calls == [1]
    assert await pipe.retry_pending() == 0


async def test_attempts_reset_after_successful_review(repo):
    pipe, jev, llm, actions, notifier = build_pipeline(
        repo, jev=FakeJevClient(JEV_RESULTS["REVIEW"]), llm=FakeLLM(LlmError("http", "500")))
    assert await pipe.process_post(make_post(TECH_TEXT)) == "llm_error"
    assert await pipe.retry_pending() == 1
    assert (await repo.get_job(1)).attempts == 2
    llm.behavior = review(85, True)
    assert await pipe.retry_pending() == 1
    job = await repo.get_job(1)
    assert job.status == JobStatus.NOTIFIED and job.attempts == 0


async def test_plain_runtime_error_from_jev_uses_fallback(repo):
    pipe, jev, llm, actions, notifier = build_pipeline(repo, jev=FakeJevClient(RuntimeError("adapter bug")))
    assert await pipe.process_post(make_post(TECH_TEXT)) == "notified"
    job = await repo.get_job(1)
    assert job.route == ROUTE_JEV_FALLBACK and llm.calls == 1
    async with repo.db.session() as s:
        usage = (await s.execute(select(JevUsage))).scalar_one()
    assert usage.error and "adapter bug" in usage.error and usage.fallback_used is True


async def test_plain_runtime_error_from_jev_fallback_disabled(repo):
    pipe, jev, llm, actions, notifier = build_pipeline(
        repo, jev=FakeJevClient(RuntimeError("adapter bug")),
        options=PipelineOptions(jev_fallback_to_openrouter=False))
    assert await pipe.process_post(make_post(TECH_TEXT)) == "jev_unavailable"
    job = await repo.get_job(1)
    assert job.status == JobStatus.JEV_UNAVAILABLE and job.attempts == 1
    assert llm.calls == 0 and notifier.calls == []
