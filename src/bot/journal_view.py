"""Journal screen builders (pure functions): text, keyboard, callback parsing, retention screen.

Callback data (< 64 bytes): ``j:<kind>:<period>:<page>`` with period in {d, w, a};
retention: ``jr:<days>``, ``jr:custom``, screen opened by ``m:jr``.
All untrusted strings (titles, channel names, reasons) go through ``html.escape``.
"""
from __future__ import annotations

import html
from datetime import datetime
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import journal as jr
from ..settings_store import retention_max, retention_min
from ..timeutil import fmt_local

PAGE_SIZE = 10
TELEGRAM_LIMIT = 4096
PERIODS = {"d": ("24 ч", 24), "w": ("7 дн", 24 * 7), "a": ("всё время", None)}
RETENTION_PRESETS = (14, 30, 60, 90, 180, 365)
EMOJI = {jr.KIND_SENT: "✅", jr.KIND_RULES: "🧹", jr.KIND_JEV: "🤖", jr.KIND_FIT: "📉",
         jr.KIND_PAID: "💰", jr.KIND_DUP: "♻️", jr.KIND_PENDING: "⏳"}
SAFE_URL_PREFIXES = ("http://", "https://", "t.me/")
RETENTION_PROMPT = "Пришлите число дней хранения журнала (целое). /cancel — отмена"
NO_ENTRIES = "Записей нет"


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def _btn(text: str, data: str) -> InlineKeyboardButton:
    assert len(data.encode("utf-8")) < 64
    return InlineKeyboardButton(text=text, callback_data=data)


def parse_journal_callback(data: str | None) -> tuple[str, str, int] | None:
    """``j:sent:w:2`` -> ("sent", "w", 2); anything unknown -> None."""
    parts = (data or "").split(":")
    if len(parts) != 4 or parts[0] != "j":
        return None
    _, kind, period, page = parts
    if kind not in jr.KINDS or period not in PERIODS or not page.isascii() or not page.isdigit() or len(page) > 6:
        return None
    return kind, period, int(page)


def journal_data(kind: str, period: str, page: int) -> str:
    return f"j:{kind}:{period}:{max(0, int(page))}"


def safe_url(url: str | None) -> str | None:
    u = (url or "").strip()
    if not u or any(c.isspace() for c in u):
        return None
    low = u.lower()
    if not low.startswith(SAFE_URL_PREFIXES):
        return None
    return u if low.startswith("http") else "https://" + u


def _clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:max(0, n - 1)] + "…"


def _entry_lines(entry, tz: str, title_max: int, reason_max: int) -> str:
    when = fmt_local(entry.received_at, tz)
    chan = f"@{_e(entry.channel_username)}" if entry.channel_username else "—"
    title = _e(_clip(entry.title, title_max)) or "(без названия)"
    url = safe_url(entry.url)
    if url:
        title = f'<a href="{html.escape(url, quote=True)}">{title}</a>'
    emoji = EMOJI.get(entry.kind, "•")
    return f"{emoji} {when} · {chan} · {title}\n   └ {_e(_clip(entry.reason, reason_max))}"


def header_text(counts_24h: dict[str, int]) -> str:
    c = counts_24h
    return (f"📜 Журнал за 24 ч: всего {c.get('all', 0)} · ✅ отправлено {c.get('sent', 0)} · "
            f"🧹 правила {c.get('rules', 0)} · 🤖 JEV {c.get('jev', 0)} · 📉 оценка {c.get('fit', 0)} · "
            f"💰 платные {c.get('paid', 0)} · ♻️ дубли {c.get('dup', 0)} · ⏳ в обработке {c.get('pending', 0)}")


def journal_text(entries: list, counts_24h: dict[str, int], kind: str, period: str, page: int,
                 pages: int, tz: str) -> str:
    """Header + filter line + entries; shrinks titles/reasons, then drops entries, to fit 4096."""
    head = header_text(counts_24h)
    head += f"\nФильтр: {_e(jr.KIND_LABELS.get(kind, kind))} · период: {PERIODS[period][0]} · стр. {page + 1}/{pages}"
    if not entries:
        return f"{head}\n\n{NO_ENTRIES}"
    for title_max, reason_max in ((100, 160), (60, 100), (35, 60), (20, 40), (10, 20)):
        body = "\n".join(_entry_lines(en, tz, title_max, reason_max) for en in entries)
        text = f"{head}\n\n{body}"
        if len(text) <= TELEGRAM_LIMIT:
            return text
    shown = list(entries)
    while shown:
        shown.pop()
        body = "\n".join(_entry_lines(en, tz, 10, 20) for en in shown)
        text = f"{head}\n\n{body}\n…"
        if len(text) <= TELEGRAM_LIMIT:
            return text
    return head[:TELEGRAM_LIMIT]


def journal_keyboard(kind: str, period: str, page: int, pages: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    kinds = list(jr.KINDS)
    for i in range(0, len(kinds), 2):
        rows.append([_btn(("• " if k == kind else "") + jr.KIND_LABELS[k], journal_data(k, period, 0))
                     for k in kinds[i:i + 2]])
    rows.append([_btn(("• " if p == period else "") + PERIODS[p][0], journal_data(kind, p, 0)) for p in PERIODS])
    nav = []
    if page > 0:
        nav.append(_btn("◀️", journal_data(kind, period, page - 1)))
    if pages > 1:
        nav.append(_btn(f"стр. {page + 1}", journal_data(kind, period, page)))
    if page < pages - 1:
        nav.append(_btn("▶️", journal_data(kind, period, page + 1)))
    if nav:
        rows.append(nav)
    rows.append([_btn("🔄 Обновить", journal_data(kind, period, page)), _btn("⚙️ Хранение", "m:jr")])
    rows.append([_btn("⬅️ Назад", "m:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def retention_text(rs) -> str:
    days = getattr(rs, "log_retention_days", 30)
    lo = retention_min(getattr(rs, "dedup_window_days", 14))
    return (f"🗑 Хранение журнала: {days} дн\n"
            f"Отправленные вакансии и вакансии с 👍/👎 хранятся всегда. "
            f"Минимум {lo} дн — окно распознавания дублей.")


def retention_keyboard(rs) -> InlineKeyboardMarkup:
    days = getattr(rs, "log_retention_days", 30)
    lo = retention_min(getattr(rs, "dedup_window_days", 14))
    hi = retention_max(getattr(rs, "dedup_window_days", 14))
    presets = [d for d in RETENTION_PRESETS if lo <= d <= hi]
    btns = [_btn(("✅ " if d == days else "") + f"{d} дн", f"jr:{d}") for d in presets]
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    rows.append([_btn("✏️ Своё значение", "jr:custom")])
    rows.append([_btn("⬅️ Назад", journal_data("all", "d", 0))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def page_count(total: int) -> int:
    return max(1, -(-int(total) // PAGE_SIZE))
