"""Plain-text statistics report shared by the /stats bot command and scripts/stats.py."""
from __future__ import annotations

from datetime import datetime

from ..db.models import JobStatus
from ..timeutil import fmt_local

# Russian labels of Job.status values; a test enumerates JobStatus.all() so none is forgotten
JOB_STATUS_LABELS = {
    JobStatus.NEW: "Новые",
    JobStatus.RULE_REJECTED: "Отсеяно правилами",
    JobStatus.JEV_REJECTED: "Отклонено JEV",
    JobStatus.JEV_UNAVAILABLE: "JEV недоступен (повтор позже)",
    JobStatus.LLM_ERROR: "Ошибка OpenRouter (повтор позже)",
    JobStatus.FIT_REJECTED: "Не подошло по оценке",
    JobStatus.ACCEPTED: "Подходит, ждёт отправки",
    JobStatus.PAID_SKIPPED: "Скрыт платный контакт",
    JobStatus.NOTIFYING: "Отправляется",
    JobStatus.NOTIFIED: "Отправлено",
    JobStatus.NOTIFY_ERROR: "Ошибка отправки (повтор позже)",
    JobStatus.NOTIFY_UNCERTAIN: "Отправка под вопросом",
    JobStatus.ERROR: "Ошибка",
}


def status_label(status: str) -> str:
    """Russian label for a status; an unknown value is shown as is."""
    return JOB_STATUS_LABELS.get(status, status)


def format_stats(stats: dict, tz: str = "Europe/Sofia") -> str:
    """All-time counters (live rows + archive of cleaned-up ones); ``tz`` formats the 'since' date."""
    g = lambda k: int(stats.get(k) or 0)  # noqa: E731
    jev_processed = g("jev_processed")
    review_calls = g("openrouter_review_calls")
    other_calls = max(0, g("openrouter_calls") - review_calls)
    jev_cost = float(stats.get("jev_cost_usd") or 0.0)
    or_cost = float(stats.get("openrouter_cost_usd") or 0.0)
    ors = f"OpenRouter: проверок {review_calls}, прочих вызовов {other_calls}"
    if jev_processed > 0:
        saved = 100 - round(review_calls * 100 / jev_processed)
        ors += f" · сэкономлено {max(0, min(100, saved))}%"
    lines = [
        "📊 Статистика за всё время",
        "",
        "📥 Поток",
        f"Получено сообщений: {g('messages_received')}",
        f"Дубли: {g('duplicates')}",
        f"Отсеяно правилами: {g('rule_rejects')}",
        "",
        "🧠 Отбор (JEV)",
        f"Проверено: {jev_processed} — подходит {g('jev_accepts')} · мимо {g('jev_rejects')} · "
        f"на проверку {g('jev_reviews')}",
        f"Ошибки JEV: {g('jev_errors')} (резерв через OpenRouter: {g('jev_fallbacks')})",
        ors,
        "",
        "📨 Уведомления",
        f"Отправлено карточек: {g('notifications')}",
        f"Скрыто платных контактов: {g('paid_contacts')}",
        f"Отзывы: 👍 {g('feedback_up')} · 👎 {g('feedback_down')}",
        "",
        "💵 Расходы (оценка)",
        f"JEV ${jev_cost:.4f} · OpenRouter ${or_cost:.4f} · всего ${jev_cost + or_cost:.4f}",
    ]
    by_status = stats.get("jobs_by_status") or {}
    if by_status:
        lines += ["", "📂 Заказы по статусам"]
        for status, n in sorted(by_status.items(), key=lambda kv: -kv[1]):
            lines.append(f"{status_label(status)}: {n}")
    since = stats.get("since")
    if isinstance(since, datetime):
        lines += ["", f"Подробный журнал хранится с {fmt_local(since, tz, '%d.%m.%Y')}"]
    return "\n".join(lines)
