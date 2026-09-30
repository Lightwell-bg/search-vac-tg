"""Data-access layer. Every method opens its own session/transaction; errors propagate."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

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


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db

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
    ) -> int:
        async with self.db.session() as s:
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
            msg = await s.get(Message, primary_message_id)
            if msg is not None:
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
            return stats
