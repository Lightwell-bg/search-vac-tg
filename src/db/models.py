"""SQLAlchemy ORM models (SQLite, async via aiosqlite).

Data flow: a Telegram post is stored in ``messages``. The first copy of a vacancy
creates a row in ``jobs``; later copies (reposts, other channels) are linked to
the same job through ``job_sources`` and marked ``is_duplicate`` on the message.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class JobStatus:
    """Processing status of a job (string constants, stored as text)."""

    NEW = "new"
    RULE_REJECTED = "rule_rejected"
    JEV_REJECTED = "jev_rejected"
    JEV_UNAVAILABLE = "jev_unavailable"   # JEV failed and fallback disabled -> retried later
    LLM_ERROR = "llm_error"               # OpenRouter failed during review -> retried later
    FIT_REJECTED = "fit_rejected"         # final decision: not a fit
    ACCEPTED = "accepted"                 # fit, contact not resolved yet
    PAID_SKIPPED = "paid_skipped"         # fit, but contact is paid and SHOW_PAID_CONTACT=false
    NOTIFYING = "notifying"               # send claimed; if seen on retry the send is uncertain
    NOTIFIED = "notified"
    NOTIFY_ERROR = "notify_error"         # send raised before Telegram accepted it -> retried
    NOTIFY_UNCERTAIN = "notify_uncertain" # crashed mid-send; never auto-resent (no duplicates)
    ERROR = "error"

    # NEW is included so a job interrupted by an unexpected exception/crash is resumed
    RETRYABLE = (NEW, JEV_UNAVAILABLE, LLM_ERROR, NOTIFY_ERROR, ACCEPTED, NOTIFYING)


# contact_status value written before the resolver may press a button (durable claim):
# a job found in this state on retry is never clicked again.
CONTACT_RESOLVING = "resolving"


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str | None] = mapped_column(String(256))
    last_message_id: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("channel_tg_id", "message_id", name="uq_channel_message"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_username: Mapped[str | None] = mapped_column(String(64))
    message_id: Mapped[int] = mapped_column(Integer)
    url: Mapped[str | None] = mapped_column(String(512))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    original_text: Mapped[str] = mapped_column(Text, default="")
    normalized_text: Mapped[str] = mapped_column(Text, default="")
    buttons: Mapped[list] = mapped_column(JSON, default=list)       # [ButtonInfo.as_dict()]
    extracted: Mapped[dict] = mapped_column(JSON, default=dict)     # contacts/budget/techs
    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), index=True)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    primary_message_id: Mapped[int | None] = mapped_column(Integer)  # messages.id of first copy
    text_hash: Mapped[str] = mapped_column(String(64), index=True)
    dedup_key: Mapped[str] = mapped_column(Text, default="")
    normalized_text: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str | None] = mapped_column(String(256))
    budget: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.NEW, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    rules_result: Mapped[dict | None] = mapped_column(JSON)
    jev_result: Mapped[dict | None] = mapped_column(JSON)
    llm_result: Mapped[dict | None] = mapped_column(JSON)
    route: Mapped[str | None] = mapped_column(String(64))   # "rules", "jev", "jev->openrouter", ...
    category: Mapped[str | None] = mapped_column(String(64))
    fit_score: Mapped[int | None] = mapped_column(Integer)
    decision_reason: Mapped[str | None] = mapped_column(Text)
    contact_status: Mapped[str | None] = mapped_column(String(32))
    contact_value: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class JobSource(Base):
    __tablename__ = "job_sources"
    __table_args__ = (UniqueConstraint("job_id", "message_row_id", name="uq_job_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    message_row_id: Mapped[int] = mapped_column(ForeignKey("messages.id"))
    channel_username: Mapped[str | None] = mapped_column(String(64))
    url: Mapped[str | None] = mapped_column(String(512))
    match_kind: Mapped[str] = mapped_column(String(16), default="original")  # original|hash|fuzzy
    similarity: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))     # username|tg_link|email|url|bot_deeplink|phone
    value: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32))   # text|url_button|callback|edited_message
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    tg_message_id: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32), default="job")   # job|application
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    value: Mapped[str] = mapped_column(String(8))     # up|down
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)  # rules/jev/llm/final at click time
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class JevUsage(Base):
    __tablename__ = "jev_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int | None] = mapped_column(Integer, index=True)
    purpose: Mapped[str] = mapped_column(String(32), default="job_classification")
    model: Mapped[str | None] = mapped_column(String(128))
    decision: Mapped[str | None] = mapped_column(String(16))
    confidence: Mapped[float | None] = mapped_column(Float)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LlmUsage(Base):
    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int | None] = mapped_column(Integer, index=True)
    purpose: Mapped[str] = mapped_column(String(32))  # job_review|application_generation|profile_building
    model: Mapped[str] = mapped_column(String(128))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
