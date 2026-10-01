"""Processing journal: entry type, human-readable reasons and retention cleanup.

One journal entry describes one stored Telegram post (``messages`` LEFT JOIN ``jobs``).
The SQL lives in ``Repository.journal`` / ``journal_counts``; this module turns the raw
job data into short Russian text and runs the periodic cleanup.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

log = logging.getLogger("journal")

# filter values for Repository.journal(kind=...)
KIND_ALL = "all"
KIND_SENT = "sent"
KIND_RULES = "rules"
KIND_JEV = "jev"
KIND_FIT = "fit"
KIND_PAID = "paid"
KIND_DUP = "dup"
KIND_PENDING = "pending"
KINDS = (KIND_ALL, KIND_SENT, KIND_RULES, KIND_JEV, KIND_FIT, KIND_PAID, KIND_DUP, KIND_PENDING)
KIND_LABELS = {
    KIND_ALL: "Все", KIND_SENT: "Отправлено", KIND_RULES: "Отсеяно правилами",
    KIND_JEV: "Отклонено JEV", KIND_FIT: "Не подошло по оценке", KIND_PAID: "Платный контакт",
    KIND_DUP: "Дубли", KIND_PENDING: "В обработке / ошибки",
}

TITLE_MAX = 100
CLEANUP_INTERVAL_SEC = 6 * 3600

CATEGORY_LABELS = {
    "telegram_automation": "Telegram-боты и автоматизация",
    "business_automation": "автоматизация и интеграции",
    "ai_llm": "AI/LLM",
    "web_backend": "Backend / Python",
    "wordpress": "WordPress",
    "parsing": "парсинг",
    "other_tech": "другая разработка",
    "non_tech": "нетехническое",
}
_DECISION_LABELS = {"reject": "не подходит", "accept": "подходит", "review": "нужна проверка"}
_SCORE_RE = re.compile(r"score\s+(\d+)\s*<\s*(\d+)")


@dataclass
class JournalEntry:
    message_row_id: int
    received_at: datetime            # aware UTC
    channel_username: str | None
    url: str | None                  # for duplicates: the original job's post
    title: str
    kind: str                        # one of KINDS except "all"
    reason: str                      # short Russian text
    fit_score: int | None
    route: str | None
    contact_status: str | None
    # raw data used by describe(); not meant for display
    job_id: int | None = None
    status: str | None = None
    decision_reason: str | None = field(default=None, repr=False)
    jev_result: dict | None = field(default=None, repr=False)
    llm_result: dict | None = field(default=None, repr=False)


def make_title(job_title: str | None, normalized_text: str | None) -> str:
    """Job title, else the first non-empty line of the text, at most TITLE_MAX characters."""
    t = (job_title or "").strip()
    if not t:
        t = next((ln.strip() for ln in (normalized_text or "").splitlines() if ln.strip()), "")
    t = " ".join(t.split())
    return t if len(t) <= TITLE_MAX else t[:TITLE_MAX - 1] + "…"


def _rule_reason(raw: str | None) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw == "too_short":
        return "слишком короткий текст"
    if raw == "no_job_signal":
        return "не похоже на вакансию"
    m = re.match(r"^(?:strong_)?negative_only:\s*(.*)$", raw)
    if m:
        return f"нет технических слов, есть: {m.group(1)}" if m.group(1) else "нет технических слов"
    return None


def _jev_reason(jev: dict | None, fallback: str | None) -> str:
    if not jev:
        return fallback or "JEV: не подходит"
    decision = _DECISION_LABELS.get(str(jev.get("raw_decision") or jev.get("decision")), "не подходит")
    parts = []
    conf = jev.get("confidence")
    if isinstance(conf, (int, float)):
        parts.append(f"уверенность {conf:.2f}")
    fit = jev.get("fit_raw")
    if isinstance(fit, (int, float)):
        parts.append(f"совпадение {fit:.1f}/3")
    cat = jev.get("category")
    if cat:
        parts.append(CATEGORY_LABELS.get(str(cat), str(cat)))
    return f"JEV: {decision}" + (f" ({', '.join(parts)})" if parts else "")


def describe(entry: JournalEntry) -> str:
    """Short Russian explanation of the outcome; falls back to the stored decision_reason."""
    fallback = (entry.decision_reason or "").strip() or None
    k = entry.kind
    if k == KIND_DUP:
        return f"дубль вакансии #{entry.job_id}" if entry.job_id else "дубль вакансии"
    if k == KIND_SENT:
        return "отправлено" + (f", оценка {entry.fit_score}" if entry.fit_score is not None else "")
    if k == KIND_PAID:
        return "контакт платный"
    if k == KIND_RULES:
        if entry.job_id is None:
            return "нет текста"
        return _rule_reason(entry.decision_reason) or fallback or "отсеяно правилами"
    if k == KIND_JEV:
        return _jev_reason(entry.jev_result, fallback)
    if k == KIND_FIT:
        score = entry.fit_score
        m = _SCORE_RE.search(entry.decision_reason or "")
        text = "не подошло"
        if score is not None:
            text = f"оценка {score}" + (f" < порога {m.group(2)}" if m else "")
        llm_reason = ((entry.llm_result or {}).get("reason") or "").strip()
        if llm_reason:
            return f"{text}: {llm_reason[:200]}"
        if m is None and fallback:
            return f"{text}: {fallback[:200]}"
        return text
    return _pending_reason(entry, fallback)


_PENDING_LABELS = {
    "new": "в обработке",
    "jev_unavailable": "JEV недоступен, повторим позже",
    "llm_error": "ошибка OpenRouter, повторим позже",
    "accepted": "подходит, ждёт отправки",
    "notifying": "отправляется",
    "notify_error": "ошибка отправки, повторим позже",
    "notify_uncertain": "отправка не подтверждена",
    "error": "ошибка обработки",
}


def _pending_reason(entry: JournalEntry, fallback: str | None) -> str:
    if entry.job_id is None:
        return "ещё не обработано"
    return _PENDING_LABELS.get(entry.status or "", fallback or "в обработке")


async def cleanup_old(repo, days: int, in_progress: set[int] | None = None) -> dict[str, int]:
    """Delete journal data older than ``days`` (one transaction). Jobs that were notified or
    have feedback, and jobs in a retryable status or ``in_progress``, are kept with their
    primary message, sources, contacts, notifications and feedback. Channels and settings
    are never touched. Returns per-table counts (``messages``, ``jobs``, ...)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
    counts = await repo.delete_older_than(cutoff, protected_job_ids=set(in_progress or ()))
    log.info("JOURNAL_CLEANUP deleted %s messages, %s jobs", counts.get("messages", 0), counts.get("jobs", 0))
    return counts

