"""Job pipeline: message -> normalize -> dedup -> rules -> JEV -> [OpenRouter] -> decision
-> contact resolver -> notification.

OpenRouter is called only when JEV says REVIEW, or JEV failed and
JEV_FALLBACK_TO_OPENROUTER=true. JEV ACCEPT/REJECT never reach OpenRouter.

Durability: a saved message without a job ("orphan") and a job in any retryable
status (including ``new``) are picked up again by ``retry_pending``. External side
effects are claimed in the DB *before* they happen, so a crash never repeats them:
``contact_status='resolving'`` before a button click, ``status='notifying'`` before
sending the card. One bad post never stops the monitor: entry points catch and log.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from ..db.models import CONTACT_RESOLVING, Job, JobStatus
from ..jev.classifier import JevClassifier
from ..journal import cleanup_old
from ..jev.schemas import ACCEPT, REJECT, REVIEW, JevDecision, JevError
from ..llm.openrouter import OpenRouterClient
from ..llm.schemas import LlmError, ReviewResult
from ..profile.loader import compact_profile
from ..telegram.contact_resolver import PAID, UNKNOWN, ContactResolver, ContactResult
from ..telegram.parser import ButtonInfo, ContactInfo, RawPost, normalize_post
from .deduplicator import Deduplicator
from .rules import RuleFilter, RuleResult
from .scorer import decide

log = logging.getLogger("pipeline")

ROUTE_RULES = "rules"
ROUTE_JEV = "JEV"
ROUTE_JEV_REVIEW = "JEV → OpenRouter review"
ROUTE_JEV_FALLBACK = "JEV error → OpenRouter"

ORPHAN_GRACE = timedelta(minutes=2)
_NOTIFY_STAGE = (JobStatus.ACCEPTED, JobStatus.NOTIFY_ERROR, JobStatus.NOTIFYING)


class Notifier(Protocol):
    async def notify_job(self, job_id: int) -> tuple[int, int | None]:
        """Send the job card to the owner. Returns (chat_id, telegram message id)."""


@dataclass
class PipelineOptions:
    notify_score: int = 65
    high_fit_score: int = 80
    jev_fallback_to_openrouter: bool = True
    show_paid_contact: bool = False
    retry_limit: int = 5
    notifications_paused: bool = False   # runtime switch from the bot: hold cards, keep ACCEPTED


class Pipeline:
    def __init__(self, repo, rules: RuleFilter, dedup: Deduplicator, jev: JevClassifier,
                 llm: OpenRouterClient | None, resolver: ContactResolver, notifier: Notifier | None,
                 profile: dict, options: PipelineOptions):
        self.repo = repo
        self.rules = rules
        self.dedup = dedup
        self.jev = jev
        self.llm = llm
        self.resolver = resolver
        self.notifier = notifier
        self.profile = profile
        self.compact = compact_profile(profile)
        self.opt = options
        self._dedup_lock = asyncio.Lock()
        self._in_progress: set[int] = set()
        self._retry_lock = asyncio.Lock()
        self._last_round_ids: list[int] = []

    def set_profile(self, profile: dict) -> None:
        self.profile = profile
        self.compact = compact_profile(profile)

    # ------------------------------------------------------------------ entry points

    async def process_post(self, post: RawPost) -> str:
        """Handle one channel post. Returns the outcome; never raises.

        ``"error"`` means the post may not have been stored durably (the listener must
        not move its cursor past it)."""
        try:
            if await self.repo.message_exists(post.channel_tg_id, post.message_id):
                return "seen"
            norm = normalize_post(post, self.rules.ignore_contacts)
            rules = self.rules.evaluate(norm.text)
            msg_row, created = await self.repo.save_message(
                channel_tg_id=post.channel_tg_id, channel_username=post.channel_username,
                message_id=post.message_id, url=post.url, posted_at=post.date,
                original_text=post.text, normalized_text=norm.text,
                buttons=[b.as_dict() for b in post.buttons],
                extracted={"contacts": [c.as_dict() for c in norm.contacts], "budget": norm.budget,
                           "title": norm.title, "technologies": rules.technologies})
        except Exception:
            log.exception("ERROR storing %s/%s", post.channel_username, post.message_id)
            return "error"
        if not created:
            return "seen"
        log.info("NEW_MESSAGE %s/%s", post.channel_username, post.message_id)
        # from here on the post is durable: failures are recovered by retry_pending
        try:
            return await self._ingest(msg_row, post, norm, rules)
        except Exception:
            log.exception("ERROR processing %s/%s (will be retried)", post.channel_username, post.message_id)
            return "failed"

    async def retry_pending(self) -> int:
        """Recover orphan messages and re-run jobs in retryable statuses (serialized)."""
        async with self._retry_lock:
            return await self._retry_pending()

    async def cleanup_journal(self, days: int) -> dict[str, int]:
        """Retention cleanup. Holds the dedup lock so no duplicate gets linked to a job that is
        being deleted; jobs currently processed are skipped."""
        async with self._dedup_lock:
            return await cleanup_old(self.repo, days, in_progress=set(self._in_progress))

    async def flush_backlog(self) -> int:
        """Send everything held while notifications were paused; call right after unpausing.
        Repeats while full batches keep coming (retry_pending takes 50 jobs at a time)."""
        total = 0
        prev: set[int] | None = None
        while True:
            done = await self.retry_pending()
            total += done
            ids = set(self._last_round_ids)
            if done == 0 or ids == prev:
                break  # no progress: nothing (new) was processed this round
            prev = ids
        return total

    async def _retry_pending(self) -> int:
        done = 0
        self._last_round_ids = []
        before = datetime.now(timezone.utc) - ORPHAN_GRACE
        for msg in await self.repo.orphan_messages(before, 50):
            try:
                post = _post_from_row(msg)
                norm = normalize_post(post, self.rules.ignore_contacts)
                await self._ingest(msg.id, post, norm, self.rules.evaluate(norm.text))
                done += 1
            except Exception:
                log.exception("ERROR recovering orphan message %s", msg.id)
        statuses = list(JobStatus.RETRYABLE)
        if self.opt.notifications_paused:
            # held until unpaused; excluded in SQL so they cannot starve other retries
            statuses = [x for x in statuses if x not in _NOTIFY_STAGE]
        for job in await self.repo.jobs_by_status(statuses, self.opt.retry_limit):
            if job.id in self._in_progress:
                continue
            if job.status == JobStatus.NEW and job.updated_at and _aware(job.updated_at) > before:
                continue  # probably still being processed by a just-started task
            try:
                await self._resume(job)
                done += 1
                self._last_round_ids.append(job.id)
            except Exception as e:
                log.exception("ERROR retrying job %s", job.id)
                await self._fail_stage(job.id, e)
        return done

    # ------------------------------------------------------------------ internals

    async def _ingest(self, msg_row: int, post: RawPost, norm, rules: RuleResult) -> str:
        if not norm.text:  # media-only post; orphan recovery skips empty texts too
            log.info("RULE_REJECT %s/%s empty text", post.channel_username, post.message_id)
            return "empty"
        async with self._dedup_lock:  # dedup + create must not interleave (cleanup holds it too)
            if not await self.repo.message_row_exists(msg_row):
                # deleted by retention cleanup after an orphan was selected: nothing to link
                log.info("SKIP %s/%s: message row %s no longer exists", post.channel_username,
                         post.message_id, msg_row)
                return "gone"
            match = await self.dedup.find(norm.text_hash, norm.dedup_key)
            if match:
                await self.repo.link_duplicate(match.job_id, msg_row, match.kind, match.similarity)
                log.info("DUPLICATE %s/%s -> job %s (%s %s)", post.channel_username,
                         post.message_id, match.job_id, match.kind, match.similarity)
                return "duplicate"
            job_id = await self.repo.create_job(msg_row, norm.text_hash, norm.text, norm.title,
                                                norm.budget, dedup_key=norm.dedup_key)
            if job_id is None:
                return "gone"
        return await self._run(job_id, self._evaluate(job_id, post, norm.text, norm.contacts, rules))

    async def _run(self, job_id: int, coro) -> str:
        """Run one job stage chain with the in-progress guard; unexpected errors keep the
        job in its (retryable) status and count an attempt."""
        self._in_progress.add(job_id)
        try:
            return await coro
        except Exception as e:
            log.exception("ERROR job %s", job_id)
            await self._fail_stage(job_id, e)
            return "failed"
        finally:
            self._in_progress.discard(job_id)

    async def _fail_stage(self, job_id: int, e: Exception) -> None:
        try:
            await self.repo.increment_attempts(job_id)
            await self.repo.update_job(job_id, last_error=f"{type(e).__name__}: {e}"[:500])
        except Exception:
            log.exception("ERROR recording failure of job %s", job_id)

    async def _resume(self, job: Job) -> str:
        msg = await self.repo.get_primary_message(job.id)
        if msg is None:
            await self.repo.update_job(job.id, status=JobStatus.ERROR, last_error="primary message missing")
            return "error"
        post = _post_from_row(msg)
        contacts = [ContactInfo(**c) for c in (msg.extracted or {}).get("contacts", [])]
        if job.status == JobStatus.NOTIFYING:
            # crashed between claiming and confirming the send: the card may have been
            # delivered, so never resend automatically
            await self.repo.update_job(job.id, status=JobStatus.NOTIFY_UNCERTAIN,
                                       last_error="interrupted during send; not resent")
            log.warning("ERROR job %s notification state uncertain, not resent", job.id)
            return "notify_uncertain"
        if job.status in (JobStatus.ACCEPTED, JobStatus.NOTIFY_ERROR):
            return await self._run(job.id, self._deliver(job.id, post, contacts, job.contact_status))
        rules = self.rules.evaluate(job.normalized_text)
        jev = _jev_from_dict(job.jev_result) if job.status == JobStatus.LLM_ERROR else None
        return await self._run(job.id, self._evaluate(job.id, post, job.normalized_text, contacts, rules, jev))

    async def _evaluate(self, job_id: int, post: RawPost, text: str, contacts: list[ContactInfo],
                        rules: RuleResult, jev: JevDecision | None = None) -> str:
        # 1. deterministic rules
        if rules.verdict == "reject":
            await self.repo.update_job(job_id, status=JobStatus.RULE_REJECTED, route=ROUTE_RULES,
                                       rules_result=rules.as_dict(), decision_reason=rules.reason)
            log.info("RULE_REJECT job %s: %s", job_id, rules.reason)
            return "rule_rejected"
        if rules.strong_accept:
            log.info("RULE_ACCEPT job %s: %s", job_id, ", ".join(rules.technologies))
        await self.repo.update_job(job_id, rules_result=rules.as_dict())

        # 2. JEV semantic filter
        route = ROUTE_JEV
        if jev is None:
            try:
                jev = await self.jev.classify(text, self.compact, rules.technologies)
            except Exception as e:  # JevError, or anything unexpected from the adapter
                err = e if isinstance(e, JevError) else JevError("unexpected", f"{type(e).__name__}: {e}")
                await self.repo.log_jev_usage(job_id=job_id, purpose="job_classification",
                                              model=self.jev.client.model, duration_ms=err.duration_ms,
                                              error=str(err)[:500],
                                              fallback_used=self.opt.jev_fallback_to_openrouter)
                log.warning("JEV_ERROR job %s: %s", job_id, err)
                if not self.opt.jev_fallback_to_openrouter:
                    await self.repo.increment_attempts(job_id)
                    await self.repo.update_job(job_id, status=JobStatus.JEV_UNAVAILABLE, last_error=str(err)[:500])
                    return "jev_unavailable"
                route = ROUTE_JEV_FALLBACK
            else:
                u = jev.usage
                await self.repo.log_jev_usage(job_id=job_id, purpose="job_classification", model=u.model,
                                              decision=jev.decision, confidence=jev.confidence,
                                              duration_ms=u.duration_ms, input_tokens=u.input_tokens,
                                              output_tokens=u.output_tokens, cost_usd=u.cost_usd)
                await self.repo.update_job(job_id, jev_result=jev.as_dict(), category=jev.category)
                log.info("JEV_%s job %s: %s", jev.decision.upper(), job_id, jev.reason)
                if jev.decision == REJECT:
                    await self.repo.update_job(job_id, status=JobStatus.JEV_REJECTED, route=ROUTE_JEV,
                                               fit_score=jev.fit_score, decision_reason=jev.reason)
                    return "jev_rejected"
                if jev.decision == REVIEW:
                    route = ROUTE_JEV_REVIEW
        else:
            route = ROUTE_JEV_REVIEW  # resumed after an OpenRouter failure

        # 3. OpenRouter only for REVIEW or JEV failure with fallback
        review: ReviewResult | None = None
        if jev is None or jev.decision != ACCEPT:
            review = await self._review(job_id, text, jev)
            if review is None:
                return "llm_error"

        # 4. final decision
        final = decide(rules, jev, review, self.opt.notify_score)
        await self.repo.update_job(job_id, route=route, fit_score=final.score,
                                   decision_reason=final.reason[:1000], category=final.category,
                                   llm_result=review.model_dump() if review else None)
        if not final.accept:
            await self.repo.update_job(job_id, status=JobStatus.FIT_REJECTED)
            log.info("FIT_REJECTED job %s score=%s route=%s: %s", job_id, final.score, route, final.reason)
            return "fit_rejected"
        # attempts are reset once, when the job leaves the classification stages for good
        # (resetting after every JEV/LLM success would let a later failing stage loop forever)
        await self.repo.update_job(job_id, status=JobStatus.ACCEPTED, attempts=0,
                                   rules_result={**rules.as_dict(), "relevant_skills": final.relevant_skills})
        log.info("FIT_ACCEPTED job %s score=%s route=%s", job_id, final.score, route)

        # 5. contact + notification
        return await self._deliver(job_id, post, contacts, None)

    async def _review(self, job_id: int, text: str, jev: JevDecision | None) -> ReviewResult | None:
        if self.llm is None:
            await self.repo.increment_attempts(job_id)
            await self.repo.update_job(job_id, status=JobStatus.LLM_ERROR, last_error="OpenRouter not configured")
            return None
        jev_short = jev.short() if jev else "unavailable (error)"
        try:
            review, usage = await self.llm.review_job(self.compact, text, jev_short)
        except Exception as e:  # LlmError, or anything unexpected from the adapter
            err = e if isinstance(e, LlmError) else LlmError("unexpected", f"{type(e).__name__}: {e}")
            u = err.usage
            await self.repo.log_llm_usage(job_id=job_id, purpose="job_review",
                                          model=(u.model if u else self.llm.model) or "",
                                          input_tokens=u.input_tokens if u else 0,
                                          output_tokens=u.output_tokens if u else 0,
                                          cost_usd=u.cost_usd if u else 0.0,
                                          duration_ms=u.duration_ms if u else 0, error=str(err)[:500])
            await self.repo.increment_attempts(job_id)
            await self.repo.update_job(job_id, status=JobStatus.LLM_ERROR, last_error=str(err)[:500])
            log.warning("ERROR OpenRouter review job %s: %s", job_id, err)
            return None
        await self.repo.log_llm_usage(job_id=job_id, purpose="job_review", model=usage.model,
                                      input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                                      cost_usd=usage.cost_usd, duration_ms=usage.duration_ms)
        log.info("OPENROUTER_REVIEW job %s fit=%s notify=%s", job_id, review.fit_score, review.should_notify)
        return review

    async def _deliver(self, job_id: int, post: RawPost, contacts: list[ContactInfo],
                       contact_status: str | None) -> str:
        if contact_status is None or contact_status == CONTACT_RESOLVING:
            # durable claim first: if we crash after pressing a button, the retry sees
            # 'resolving' and resolves again WITHOUT pressing anything
            allow_click = contact_status is None
            await self.repo.update_job(job_id, contact_status=CONTACT_RESOLVING)
            result = await self._resolve_contact(post, contacts, allow_click)
            await self.repo.save_contacts(job_id, [c.as_dict() for c in result.contacts])
            await self.repo.update_job(job_id, contact_status=result.status, contact_value=result.value)
            log.info("CONTACT_%s job %s: %s", _contact_event(result.status), job_id, result.detail)
        else:
            job = await self.repo.get_job(job_id)
            result = ContactResult(job.contact_status, job.contact_value)
        if result.status == PAID and not self.opt.show_paid_contact:
            await self.repo.update_job(job_id, status=JobStatus.PAID_SKIPPED)
            return "paid_skipped"
        if self.opt.notifications_paused:
            # stays ACCEPTED (contact already resolved, no attempt spent): flush_backlog sends it
            log.info("NOTIFICATION_PAUSED job %s", job_id)
            return "paused"
        if self.notifier is None:
            await self.repo.increment_attempts(job_id)
            await self.repo.update_job(job_id, status=JobStatus.NOTIFY_ERROR, last_error="notifier missing")
            return "notify_error"

        await self.repo.update_job(job_id, status=JobStatus.NOTIFYING)  # send claim
        try:
            chat_id, msg_id = await self.notifier.notify_job(job_id)
        except Exception as e:
            await self.repo.increment_attempts(job_id)
            await self.repo.update_job(job_id, status=JobStatus.NOTIFY_ERROR, last_error=str(e)[:500])
            log.warning("ERROR notification job %s: %s", job_id, e)
            return "notify_error"
        # Telegram accepted the card: from here on never report/resend as failed
        try:
            await self.repo.update_job(job_id, status=JobStatus.NOTIFIED)
            await self.repo.save_notification(job_id, chat_id, msg_id, kind="job")
        except Exception:
            log.exception("ERROR recording sent notification of job %s", job_id)
        log.info("NOTIFICATION_SENT job %s", job_id)
        return "notified"

    async def _resolve_contact(self, post: RawPost, contacts: list[ContactInfo], allow_click: bool) -> ContactResult:
        try:
            return await self.resolver.resolve(post, contacts, allow_click=allow_click)
        except Exception as e:
            log.exception("ERROR contact resolver msg %s", post.message_id)
            return ContactResult(UNKNOWN, None, [], f"resolver error: {type(e).__name__}")


def _post_from_row(msg) -> RawPost:
    return RawPost(msg.channel_tg_id, msg.channel_username, msg.message_id, msg.posted_at,
                   msg.original_text, [ButtonInfo.from_dict(b) for b in (msg.buttons or [])])


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _contact_event(status: str) -> str:
    return {"direct": "DIRECT", "free": "FREE", "external_contact_flow": "EXTERNAL",
            "paid_contact": "PAID"}.get(status, "UNKNOWN")


def _jev_from_dict(d: dict | None) -> JevDecision | None:
    if not d:
        return None
    from ..jev.schemas import JevUsage
    data = dict(d)
    data["usage"] = JevUsage(**(data.get("usage") or {}))
    return JevDecision(**data)
