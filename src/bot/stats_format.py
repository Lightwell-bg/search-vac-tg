"""Plain-text statistics report shared by the /stats bot command and scripts/stats.py."""
from __future__ import annotations


def format_stats(stats: dict) -> str:
    g = lambda k: int(stats.get(k) or 0)  # noqa: E731
    jev_processed = g("jev_processed")
    review_calls = g("openrouter_review_calls")
    other_calls = max(0, g("openrouter_calls") - review_calls)
    lines = [
        "Статистика",
        f"Messages received: {g('messages_received')}",
        f"Duplicates: {g('duplicates')}",
        f"Rule rejects: {g('rule_rejects')}",
        f"JEV processed: {jev_processed}",
        f"JEV accepts: {g('jev_accepts')}",
        f"JEV rejects: {g('jev_rejects')}",
        f"JEV reviews: {g('jev_reviews')}",
        f"JEV errors: {g('jev_errors')} (fallbacks: {g('jev_fallbacks')})",
        f"OpenRouter calls: review {review_calls} / other {other_calls}",
        f"Notifications: {g('notifications')}",
        f"Paid contacts skipped: {g('paid_contacts')}",
        f"Positive feedback: {g('feedback_up')}",
        f"Negative feedback: {g('feedback_down')}",
        f"Estimated JEV cost: ${float(stats.get('jev_cost_usd') or 0.0):.4f}",
        f"Estimated OpenRouter cost: ${float(stats.get('openrouter_cost_usd') or 0.0):.4f}",
    ]
    if jev_processed > 0:
        saved = 100 - round(review_calls * 100 / jev_processed)
        lines.append(
            f"OpenRouter вызван для {review_calls} из {jev_processed} прошедших правила "
            f"({max(0, min(100, saved))}% сэкономлено)"
        )
    by_status = stats.get("jobs_by_status") or {}
    if by_status:
        lines.append("Jobs by status:")
        for status, n in sorted(by_status.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {status}: {n}")
    return "\n".join(lines)
