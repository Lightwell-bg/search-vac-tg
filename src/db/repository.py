"""Data-access layer. Every method opens its own session/transaction; errors propagate."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.orm import aliased
from sqlalchemy.exc import IntegrityError

from src import journal as jr
from src.db.database import Database
from src.db.models import (
    Channel,
    Contact,
    Feedback,
    Job,
    JobSource,
    JobStatus,
    JevUsage,
    LlmUsage,
    Message,
    Notification,
    Setting,
)


STATS_ARCHIVE_KEY = "stats_archive"
# additive counters moved into the archive when journal rows are deleted
_ARCHIVE_COUNTERS = ("messages_received", "duplicates", "rule_rejects", "jev_processed", "jev_accepts",
                     "jev_rejects", "jev_reviews", "jev_errors", "jev_fallbacks", "openrouter_calls",
                     "openrouter_review_calls", "paid_contacts")
_ARCHIVE_COSTS = ("jev_cost_usd", "openrouter_cost_usd")
COUNTS_CACHE_TTL_SEC = 60.0
_monotonic = time.monotonic   # patched in tests


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db
        # journal_counts cache: since-bucket -> (stored at, counts); cleared by cleanup
        self._counts_cache: dict[Any, tuple[float, dict[str, int]]] = {}

    # ---- channels -------------------------------------------------------
    async def upsert_channel(self, tg_id: int, username: str, title: str | None, *,
                             enabled: bool = True, click_callbacks: bool = True) -> None:
        """Insert or refresh a channel. Flags are applied only on insert: an existing
        row keeps the ``enabled``/``click_callbacks`` values set from the bot."""
        async with self.db.session() as s:
            ch = (await s.execute(select(Channel).where(Channel.tg_id == tg_id))).scalar_one_or_none()
            if ch is None:
                s.add(Channel(tg_id=tg_id, username=username, title=title,
                              enabled=enabled, click_callbacks=click_callbacks))
                try:
                    await s.commit()
                    return
                except IntegrityError:  # a concurrent insert of the same tg_id won: update instead
                    await s.rollback()
                    ch = (await s.execute(select(Channel).where(Channel.tg_id == tg_id))).scalar_one()
            ch.username = username
            ch.title = title
            await s.commit()

    async def list_channels(self) -> list[Channel]:
        async with self.db.session() as s:
            return list((await s.execute(select(Channel).order_by(Channel.id))).scalars().all())

    async def get_channel_by_username(self, username: str) -> Channel | None:
        name = username.strip().lstrip("@").lower()
        async with self.db.session() as s:
            return (await s.execute(
                select(Channel).where(func.lower(Channel.username) == name).limit(1))).scalar_one_or_none()

    async def set_channel_flags(self, tg_id: int, enabled: bool | None = None,
                                click_callbacks: bool | None = None) -> bool:
        """Update the given flags; returns False when the channel does not exist."""
        values: dict[str, Any] = {}
        if enabled is not None:
            values["enabled"] = bool(enabled)
        if click_callbacks is not None:
            values["click_callbacks"] = bool(click_callbacks)
        async with self.db.session() as s:
            if not values:
                return (await s.execute(select(Channel.id).where(Channel.tg_id == tg_id))).first() is not None
            res = await s.execute(update(Channel).where(Channel.tg_id == tg_id).values(**values))
            await s.commit()
            return bool(res.rowcount)

    async def delete_channel(self, tg_id: int) -> bool:
        """Remove only the channel row; its messages and jobs stay."""
        async with self.db.session() as s:
            res = await s.execute(delete(Channel).where(Channel.tg_id == tg_id))
            await s.commit()
            return bool(res.rowcount)

    # ---- runtime settings -----------------------------------------------
    async def get_settings(self) -> dict[str, Any]:
        async with self.db.session() as s:
            rows = (await s.execute(select(Setting))).scalars().all()
        out: dict[str, Any] = {}
        for row in rows:
            try:
                out[row.key] = json.loads(row.value)
            except ValueError:
                continue  # corrupted row: ignore, defaults apply
        return out

    async def set_setting(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        async with self.db.session() as s:
            row = await s.get(Setting, key)
            if row is None:
                s.add(Setting(key=key, value=payload))
            else:
                row.value = payload
            await s.commit()

    async def get_last_message_id(self, tg_id: int) -> int:
        async with self.db.session() as s:
            val = (await s.execute(select(Channel.last_message_id).where(Channel.tg_id == tg_id))).scalar_one_or_none()
            return int(val or 0)

    async def set_last_message_id(self, tg_id: int, message_id: int) -> None:
        async with self.db.session() as s:
            await s.execute(
                update(Channel)
                .where(Channel.tg_id == tg_id, Channel.last_message_id < message_id)
                .values(last_message_id=message_id)
            )
            await s.commit()

    # ---- messages -------------------------------------------------------
    async def message_exists(self, channel_tg_id: int, message_id: int) -> bool:
        async with self.db.session() as s:
            r = await s.execute(
                select(Message.id).where(Message.channel_tg_id == channel_tg_id, Message.message_id == message_id)
            )
            return r.scalar_one_or_none() is not None

    async def message_row_exists(self, row_id: int) -> bool:
        async with self.db.session() as s:
            return (await s.execute(select(Message.id).where(Message.id == row_id))).first() is not None

    async def save_message(self, **fields: Any) -> tuple[int, bool]:
        """Insert a message; returns (id, created). On unique-pair conflict returns the existing id."""
        async with self.db.session() as s:
            msg = Message(**fields)
            s.add(msg)
            try:
                await s.commit()
                return msg.id, True
            except IntegrityError:
                await s.rollback()
            r = await s.execute(
                select(Message.id).where(
                    Message.channel_tg_id == fields["channel_tg_id"],
                    Message.message_id == fields["message_id"],
                )
            )
            return int(r.scalar_one()), False

    # ---- jobs -----------------------------------------------------------
    async def find_job_by_hash(self, text_hash: str) -> int | None:
        async with self.db.session() as s:
            r = await s.execute(select(Job.id).where(Job.text_hash == text_hash).order_by(Job.id).limit(1))
            return r.scalar_one_or_none()

    async def recent_job_keys(self, since: datetime, limit: int) -> list[tuple[int, str]]:
        """(job id, dedup_key) for jobs created since `since`, newest first."""
        async with self.db.session() as s:
            r = await s.execute(
                select(Job.id, Job.dedup_key)
                .where(Job.created_at >= since)
                .order_by(Job.created_at.desc(), Job.id.desc())
                .limit(limit)
            )
            return [(row[0], row[1]) for row in r.all()]

    async def create_job(
        self,
        primary_message_id: int,
        text_hash: str,
        normalized_text: str,
        title: str | None,
        budget: str | None,
        dedup_key: str = "",
    ) -> int | None:
        """Create a job for ``primary_message_id`` and link the message to it. Returns None
        (nothing written) when that message no longer exists, e.g. removed by retention cleanup."""
        async with self.db.session() as s:
            msg = await s.get(Message, primary_message_id)
            if msg is None:
                return None
            job = Job(
                primary_message_id=primary_message_id,
                text_hash=text_hash,
                dedup_key=dedup_key,
                normalized_text=normalized_text,
                title=title,
                budget=budget,
            )
            s.add(job)
            await s.flush()
            msg.job_id = job.id
            s.add(
                JobSource(
                    job_id=job.id,
                    message_row_id=msg.id,
                    channel_username=msg.channel_username,
                    url=msg.url,
                    match_kind="original",
                )
            )
            await s.commit()
            return job.id

    async def orphan_messages(self, before: datetime, limit: int) -> list[Message]:
        """Stored messages that never got a job (crash between save and create_job), oldest first."""
        async with self.db.session() as s:
            r = await s.execute(
                select(Message)
                .where(
                    Message.job_id.is_(None),
                    Message.is_duplicate.is_(False),
                    Message.normalized_text != "",
                    Message.received_at < before,
                )
                .order_by(Message.id)
                .limit(limit)
            )
            return list(r.scalars().all())

    async def link_duplicate(
        self, job_id: int, message_row_id: int, match_kind: str, similarity: float | None
    ) -> None:
        async with self.db.session() as s:
            msg = await s.get(Message, message_row_id)
            if msg is None:
                raise ValueError(f"message row {message_row_id} not found")
            msg.is_duplicate = True
            msg.job_id = job_id
            exists = (
                await s.execute(
                    select(JobSource.id).where(JobSource.job_id == job_id, JobSource.message_row_id == message_row_id)
                )
            ).scalar_one_or_none()
            if exists is None:
                s.add(
                    JobSource(
                        job_id=job_id,
                        message_row_id=message_row_id,
                        channel_username=msg.channel_username,
                        url=msg.url,
                        match_kind=match_kind,
                        similarity=similarity,
                    )
                )
            await s.commit()

    async def get_job(self, job_id: int) -> Job | None:
        async with self.db.session() as s:
            return await s.get(Job, job_id)

    async def update_job(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        async with self.db.session() as s:
            await s.execute(update(Job).where(Job.id == job_id).values(**fields))
            await s.commit()

    async def increment_attempts(self, job_id: int) -> int:
        async with self.db.session() as s:
            await s.execute(update(Job).where(Job.id == job_id).values(attempts=Job.attempts + 1))
            val = (await s.execute(select(Job.attempts).where(Job.id == job_id))).scalar_one()
            await s.commit()
            return int(val)

    async def get_primary_message(self, job_id: int) -> Message | None:
        async with self.db.session() as s:
            job = await s.get(Job, job_id)
            if job is None or job.primary_message_id is None:
                return None
            return await s.get(Message, job.primary_message_id)

    async def get_job_sources(self, job_id: int) -> list[JobSource]:
        async with self.db.session() as s:
            r = await s.execute(select(JobSource).where(JobSource.job_id == job_id).order_by(JobSource.id))
            return list(r.scalars().all())

    async def jobs_by_status(self, statuses: list[str], max_attempts: int, limit: int = 50) -> list[Job]:
        async with self.db.session() as s:
            r = await s.execute(
                select(Job)
                .where(Job.status.in_(statuses), Job.attempts < max_attempts)
                .order_by(Job.id)
                .limit(limit)
            )
            return list(r.scalars().all())

    # ---- contacts / notifications / feedback ----------------------------
    async def save_contacts(self, job_id: int, contacts: list[dict]) -> None:
        if not contacts:
            return
        async with self.db.session() as s:
            for c in contacts:
                s.add(Contact(job_id=job_id, kind=c["kind"], value=c["value"], source=c.get("source", "text")))
            await s.commit()

    async def save_notification(self, job_id: int, chat_id: int, tg_message_id: int | None, kind: str = "job") -> None:
        async with self.db.session() as s:
            s.add(Notification(job_id=job_id, chat_id=chat_id, tg_message_id=tg_message_id, kind=kind))
            await s.commit()

    async def save_feedback(self, job_id: int, value: str, snapshot: dict) -> None:
        async with self.db.session() as s:
            s.add(Feedback(job_id=job_id, value=value, snapshot=snapshot))
            await s.commit()

    # ---- usage ----------------------------------------------------------
    async def log_jev_usage(self, **fields: Any) -> None:
        async with self.db.session() as s:
            s.add(JevUsage(**fields))
            await s.commit()

    async def log_llm_usage(self, **fields: Any) -> None:
        async with self.db.session() as s:
            s.add(LlmUsage(**fields))
            await s.commit()

    # ---- stats ----------------------------------------------------------
    async def get_stats(self) -> dict:
        async with self.db.session() as s:
            async def count(stmt) -> int:
                return int((await s.execute(stmt)).scalar_one() or 0)

            def cnt(model, *conds):
                q = select(func.count()).select_from(model)
                return q.where(*conds) if conds else q

            stats: dict[str, Any] = {
                "messages_received": await count(cnt(Message)),
                "duplicates": await count(cnt(Message, Message.is_duplicate.is_(True))),
                "rule_rejects": await count(cnt(Job, Job.status == JobStatus.RULE_REJECTED)),
                "jev_processed": await count(cnt(JevUsage, JevUsage.purpose == "job_classification")),
                "jev_accepts": await count(cnt(JevUsage, JevUsage.decision == "accept")),
                "jev_rejects": await count(cnt(JevUsage, JevUsage.decision == "reject")),
                "jev_reviews": await count(cnt(JevUsage, JevUsage.decision == "review")),
                "jev_errors": await count(cnt(JevUsage, JevUsage.error.is_not(None))),
                "jev_fallbacks": await count(cnt(JevUsage, JevUsage.fallback_used.is_(True))),
                "openrouter_calls": await count(cnt(LlmUsage)),
                "openrouter_review_calls": await count(cnt(LlmUsage, LlmUsage.purpose == "job_review")),
                "notifications": await count(cnt(Notification, Notification.kind == "job")),
                "feedback_up": await count(cnt(Feedback, Feedback.value == "up")),
                "feedback_down": await count(cnt(Feedback, Feedback.value == "down")),
                "paid_contacts": await count(cnt(Job, Job.contact_status == "paid_contact")),
            }
            stats["jev_cost_usd"] = float(
                (await s.execute(select(func.coalesce(func.sum(JevUsage.cost_usd), 0.0)))).scalar_one() or 0.0
            )
            stats["openrouter_cost_usd"] = float(
                (await s.execute(select(func.coalesce(func.sum(LlmUsage.cost_usd), 0.0)))).scalar_one() or 0.0
            )
            rows = (await s.execute(select(Job.status, func.count()).group_by(Job.status))).all()
            stats["jobs_by_status"] = {r[0]: int(r[1]) for r in rows}
            # the detailed journal is trimmed by retention: "since" is where it starts
            oldest = (await s.execute(select(func.min(Message.received_at)))).scalar_one_or_none()
            stats["since"] = _aware(oldest) if oldest else None
            # all-time = live rows + counters archived by cleanup
            archive = _load_archive(await s.get(Setting, STATS_ARCHIVE_KEY))
            for k in _ARCHIVE_COUNTERS:
                stats[k] += int(archive.get(k, 0))
            for k in _ARCHIVE_COSTS:
                stats[k] += float(archive.get(k, 0.0))
            for status, n in (archive.get("jobs_by_status") or {}).items():
                stats["jobs_by_status"][status] = stats["jobs_by_status"].get(status, 0) + int(n)
            return stats

    # ---- journal ---------------------------------------------------------
    @staticmethod
    def _kind_expr():
        """SQL CASE giving every message (LEFT JOIN jobs) its journal kind."""
        return case(
            (Message.is_duplicate.is_(True), jr.KIND_DUP),
            (Job.id.is_(None) & (Message.normalized_text == ""), jr.KIND_RULES),
            (Job.status == JobStatus.NOTIFIED, jr.KIND_SENT),
            (Job.status == JobStatus.RULE_REJECTED, jr.KIND_RULES),
            (Job.status == JobStatus.JEV_REJECTED, jr.KIND_JEV),
            (Job.status == JobStatus.FIT_REJECTED, jr.KIND_FIT),
            (Job.status == JobStatus.PAID_SKIPPED, jr.KIND_PAID),
            else_=jr.KIND_PENDING,
        )

    async def journal(self, since: datetime | None, kind: str = "all", limit: int = 10,
                      offset: int = 0) -> list[jr.JournalEntry]:
        """One entry per stored message, newest first. ``kind`` is one of journal.KINDS."""
        if kind not in jr.KINDS:
            raise ValueError(f"unknown journal kind: {kind!r}")
        orig = aliased(Message)
        kind_expr = self._kind_expr()
        stmt = (
            select(Message, Job, orig.url, kind_expr.label("kind"))
            .select_from(Message)
            .outerjoin(Job, Message.job_id == Job.id)
            .outerjoin(orig, Job.primary_message_id == orig.id)
            .order_by(Message.received_at.desc(), Message.id.desc())
            .limit(max(0, int(limit))).offset(max(0, int(offset)))
        )
        if since is not None:
            stmt = stmt.where(Message.received_at >= since)
        if kind != jr.KIND_ALL:
            stmt = stmt.where(kind_expr == kind)
        async with self.db.session() as s:
            rows = (await s.execute(stmt)).all()
        out: list[jr.JournalEntry] = []
        for msg, job, orig_url, k in rows:
            is_dup = k == jr.KIND_DUP
            entry = jr.JournalEntry(
                message_row_id=msg.id,
                received_at=_aware(msg.received_at),
                channel_username=msg.channel_username,
                url=(orig_url or msg.url) if is_dup else msg.url,
                title=jr.make_title(job.title if job else None,
                                    (job.normalized_text if job else None) or msg.normalized_text),
                kind=k, reason="",
                fit_score=job.fit_score if job else None,
                route=job.route if job else None,
                contact_status=job.contact_status if job else None,
                job_id=job.id if job else None,
                status=job.status if job else None,
                decision_reason=job.decision_reason if job else None,
                jev_result=job.jev_result if job else None,
                llm_result=job.llm_result if job else None,
            )
            entry.reason = jr.describe(entry)
            out.append(entry)
        return out

    async def journal_counts(self, since: datetime | None) -> dict[str, int]:
        """Messages per kind (every kind present, plus ``all``). Cached for 60 s per
        (since rounded to the minute) and cleared by cleanup, so paging does not rescan the table."""
        key = None if since is None else int(since.timestamp() // 60)
        hit = self._counts_cache.get(key)
        if hit is not None and _monotonic() - hit[0] < COUNTS_CACHE_TTL_SEC:
            return dict(hit[1])
        counts = await self._journal_counts(since)
        self._counts_cache[key] = (_monotonic(), counts)
        return dict(counts)

    async def _journal_counts(self, since: datetime | None) -> dict[str, int]:
        kind_expr = self._kind_expr()
        stmt = (select(kind_expr, func.count()).select_from(Message)
                .outerjoin(Job, Message.job_id == Job.id).group_by(kind_expr))
        if since is not None:
            stmt = stmt.where(Message.received_at >= since)
        async with self.db.session() as s:
            rows = (await s.execute(stmt)).all()
        counts = {k: 0 for k in jr.KINDS}
        for k, n in rows:
            counts[k] = int(n)
        counts[jr.KIND_ALL] = sum(v for k, v in counts.items() if k != jr.KIND_ALL)
        return counts

    # ---- retention -------------------------------------------------------
    async def delete_older_than(self, cutoff: datetime,
                                protected_job_ids: set[int] | None = None) -> dict[str, int]:
        """Delete journal rows older than ``cutoff`` in ONE transaction (see journal.cleanup_old).

        A job is deleted only if it was created before the cutoff, none of its messages is
        newer, it is not in a status of ``JobStatus.PROTECTED_FROM_CLEANUP`` / ``RETRYABLE``,
        has no notification/feedback rows and is not in ``protected_job_ids``. Old messages go
        with their deleted job or when they have no job; EVERY message (primary or duplicate)
        of a surviving job is kept. The deleted rows' contribution to /stats is added to the
        ``stats_archive`` setting in the same transaction."""
        m2 = aliased(Message)
        protected = list(protected_job_ids or ())
        keep_conds = [
            Job.status.in_(JobStatus.PROTECTED_FROM_CLEANUP),
            Job.status.in_(JobStatus.RETRYABLE),
            Job.id.in_(select(Notification.job_id)),
            Job.id.in_(select(Feedback.job_id)),
            Job.id.in_(select(m2.job_id).where(m2.received_at >= cutoff, m2.job_id.is_not(None))),
        ]
        if protected:
            keep_conds.append(Job.id.in_(protected))
        doomed = select(Job.id).where(Job.created_at < cutoff, ~or_(*keep_conds))
        surviving = select(Job.id).where(Job.id.not_in(doomed))
        old_msgs = select(Message.id).where(
            Message.received_at < cutoff,
            or_(Message.job_id.is_(None), Message.job_id.not_in(surviving)))
        counts: dict[str, int] = {}
        async with self.db.session() as s:
            await self._archive_stats(s, doomed, old_msgs)
            res = await s.execute(delete(JobSource).where(
                or_(JobSource.job_id.in_(doomed), JobSource.message_row_id.in_(old_msgs))))
            counts["job_sources"] = res.rowcount or 0
            for name, model in (("contacts", Contact), ("jev_usage", JevUsage), ("llm_usage", LlmUsage)):
                res = await s.execute(delete(model).where(model.job_id.in_(doomed)))
                counts[name] = res.rowcount or 0
            res = await s.execute(delete(Message).where(Message.id.in_(old_msgs)))
            counts["messages"] = res.rowcount or 0
            res = await s.execute(delete(Job).where(Job.id.in_(doomed)))
            counts["jobs"] = res.rowcount or 0
            await s.commit()
        self._counts_cache.clear()
        return counts

    @staticmethod
    async def _archive_stats(s, doomed, old_msgs) -> None:
        """Add the contribution of the rows about to be deleted to the ``stats_archive`` setting
        (same session, committed together with the delete)."""
        async def count(model, *conds) -> int:
            return int((await s.execute(select(func.count()).select_from(model).where(*conds))).scalar_one() or 0)

        async def total(col, *conds) -> float:
            q = select(func.coalesce(func.sum(col), 0.0)).where(*conds)
            return float((await s.execute(q)).scalar_one() or 0.0)

        jev_in, llm_in = JevUsage.job_id.in_(doomed), LlmUsage.job_id.in_(doomed)
        add: dict[str, Any] = {
            "messages_received": await count(Message, Message.id.in_(old_msgs)),
            "duplicates": await count(Message, Message.id.in_(old_msgs), Message.is_duplicate.is_(True)),
            "rule_rejects": await count(Job, Job.id.in_(doomed), Job.status == JobStatus.RULE_REJECTED),
            "jev_processed": await count(JevUsage, jev_in, JevUsage.purpose == "job_classification"),
            "jev_accepts": await count(JevUsage, jev_in, JevUsage.decision == "accept"),
            "jev_rejects": await count(JevUsage, jev_in, JevUsage.decision == "reject"),
            "jev_reviews": await count(JevUsage, jev_in, JevUsage.decision == "review"),
            "jev_errors": await count(JevUsage, jev_in, JevUsage.error.is_not(None)),
            "jev_fallbacks": await count(JevUsage, jev_in, JevUsage.fallback_used.is_(True)),
            "openrouter_calls": await count(LlmUsage, llm_in),
            "openrouter_review_calls": await count(LlmUsage, llm_in, LlmUsage.purpose == "job_review"),
            "paid_contacts": await count(Job, Job.id.in_(doomed), Job.contact_status == "paid_contact"),
            "jev_cost_usd": await total(JevUsage.cost_usd, jev_in),
            "openrouter_cost_usd": await total(LlmUsage.cost_usd, llm_in),
        }
        by_status = (await s.execute(
            select(Job.status, func.count()).where(Job.id.in_(doomed)).group_by(Job.status))).all()
        row = await s.get(Setting, STATS_ARCHIVE_KEY)
        archive = _load_archive(row)
        for k, v in add.items():
            archive[k] = archive.get(k, 0) + v
        statuses = archive.setdefault("jobs_by_status", {})
        for status, n in by_status:
            statuses[status] = int(statuses.get(status, 0)) + int(n)
        payload = json.dumps(archive, ensure_ascii=False)
        if row is None:
            s.add(Setting(key=STATS_ARCHIVE_KEY, value=payload))
        else:
            row.value = payload


def _load_archive(row: Setting | None) -> dict[str, Any]:
    if row is None:
        return {}
    try:
        data = json.loads(row.value)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _aware(dt: datetime) -> datetime:
    """SQLite returns naive datetimes; everything stored is UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
