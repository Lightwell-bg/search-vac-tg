"""Job card text. One builder for the bot (HTML) and scripts/dry_run.py (plain text).

Every untrusted value (job text, title, contact, channel names, reasons, skills) comes from
public channels or an LLM, so in HTML mode each of them goes through ``html.escape``.
"""
from __future__ import annotations

import html as _html

TELEGRAM_LIMIT = 4096
SAFE_LIMIT = 4000          # headroom below the hard Telegram limit
TEXT_LIMIT = 1500
MAX_SKILLS = 6
MAX_REASON = 300
GENERIC_SKILLS = {"api", "бот", "bot", "интеграция", "автоматизация", "automation"}


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def relevant_skills(job) -> list[str]:
    """Skills shown under "Почему подходит": rules/LLM result, else technologies found by rules."""
    rules = job.rules_result or {}
    llm = job.llm_result or {}
    skills = rules.get("relevant_skills") or llm.get("relevant_skills")
    if not skills:
        hits = rules.get("hits") or {}
        skills = list(rules.get("technologies") or []) or (
            list(hits.get("strong_positive", [])) + list(hits.get("positive", [])))
    seen: list[str] = []
    seen_low: set[str] = set()
    for s in skills:
        s = str(s).strip()
        if s and s.lower() not in seen_low:
            seen_low.add(s.lower())
            seen.append(s)
    specific = [s for s in seen if s.lower() not in GENERIC_SKILLS]
    if len(specific) >= 2:  # generic hits add noise when there are real skills
        seen = specific
    return seen[:MAX_SKILLS]


def channel_names(job_sources, message=None) -> list[str]:
    names: list[str] = []
    for src in job_sources or []:
        n = getattr(src, "channel_username", None)
        if n and n not in names:
            names.append(n)
    if not names and message is not None and getattr(message, "channel_username", None):
        names.append(message.channel_username)
    return names


def contact_line(job) -> str:
    status = job.contact_status
    value = (job.contact_value or "").strip()
    if status == "paid_contact":
        return "⚠️ платный контакт"
    if status == "external_contact_flow":
        return f"контакт через бота: {value}" if value else "контакт через бота"
    if status in ("direct", "free") and value:
        return value
    return "контакт не найден"


def build_card(job, message, sources, settings, html: bool = True) -> str:
    """Return the card text (HTML when ``html`` else plain), always under 4096 chars."""
    esc = (lambda s: _html.escape(str(s), quote=False)) if html else (lambda s: str(s))
    bold = (lambda s: f"<b>{s}</b>") if html else (lambda s: s)

    score = int(job.fit_score or 0)
    high = score >= int(getattr(settings, "high_fit_score", 80))
    header = f"🔥 Подходящий заказ — {score}/100" if high else f"✅ Возможно подходит — {score}/100"

    head_lines = [header]
    title = (job.title or "").strip()
    if title:
        head_lines.append(bold(esc(_clip(title, 200))))

    tail_lines: list[str] = []
    skills = relevant_skills(job)
    reason = (job.decision_reason or "").strip()
    if skills or reason:
        tail_lines.append("Почему подходит:")
        if skills:
            tail_lines.extend(f"• {esc(s)}" for s in skills)
        else:
            tail_lines.append(f"• {esc(_clip(reason, MAX_REASON))}")
    if job.budget:
        tail_lines.append(f"💰 {esc(_clip(job.budget, 128))}")
    names = channel_names(sources, message)
    if names:
        tail_lines.append("📡 " + esc(", ".join(names)))
    tail_lines.append("👤 " + esc(_clip(contact_line(job), 300)))
    if job.route:
        tail_lines.append(f"🤖 Отбор: {esc(job.route)}")

    head = "\n".join(head_lines)
    tail = "\n".join(tail_lines)
    body_raw = (job.normalized_text or "").strip()
    if title and body_raw.startswith(title) and body_raw[len(title):].startswith("\n"):
        # the title is the whole first line of the post: do not print it twice
        body_raw = body_raw[len(title):].strip() or body_raw
    limit = TEXT_LIMIT
    while True:
        body = esc(_clip(body_raw, limit)) if body_raw else ""
        parts = [head] + ([body] if body else []) + [tail]
        card = "\n".join(parts)
        if len(card) <= SAFE_LIMIT or limit <= 50:
            break
        limit = max(50, limit - max(50, (len(card) - SAFE_LIMIT)))  # escaping can grow the text
    return card[:TELEGRAM_LIMIT]
