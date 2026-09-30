"""Inline keyboard of a job card. Callback data: ``c:<id>``, ``f:up|down:<id>``, ``a:<id>``."""
from __future__ import annotations

import re

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

_USERNAME = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")
_TME = re.compile(r"^(?:t\.me|telegram\.me)/\S+$", re.I)


def contact_url(value: str | None) -> str | None:
    """URL for a contact button, or None when the value is not a link (email, phone, ...)."""
    v = (value or "").strip()
    if not v or " " in v:
        return None
    low = v.lower()
    if low.startswith(("http://", "https://")):
        return v
    if _TME.match(v):
        return "https://" + v
    m = _USERNAME.match(v)
    if m and v.startswith("@"):
        return f"https://t.me/{m.group(1)}"
    return None


def callback_data(kind: str, job_id: int, arg: str | None = None) -> str:
    data = f"{kind}:{arg}:{job_id}" if arg else f"{kind}:{job_id}"
    assert len(data.encode("utf-8")) < 64
    return data


def job_keyboard(job_id: int, message_url: str | None, contact_value: str | None,
                 chosen: str | None = None) -> InlineKeyboardMarkup:
    """chosen: None | "up" | "down" marks the pressed feedback button."""
    row1: list[InlineKeyboardButton] = []
    if message_url:
        row1.append(InlineKeyboardButton(text="🔗 Открыть вакансию", url=message_url))
    if contact_value and contact_value.strip():
        url = contact_url(contact_value)
        if url:
            row1.append(InlineKeyboardButton(text="👤 Контакт", url=url))
        else:
            row1.append(InlineKeyboardButton(text="👤 Контакт", callback_data=callback_data("c", job_id)))
    up = "✅ 👍 Подходит" if chosen == "up" else "👍 Подходит"
    down = "✅ 👎 Мимо" if chosen == "down" else "👎 Мимо"
    rows = []
    if row1:
        rows.append(row1)
    rows.append([
        InlineKeyboardButton(text=up, callback_data=callback_data("f", job_id, "up")),
        InlineKeyboardButton(text=down, callback_data=callback_data("f", job_id, "down")),
    ])
    rows.append([InlineKeyboardButton(text="✍️ Сделать отклик", callback_data=callback_data("a", job_id))])
    return InlineKeyboardMarkup(inline_keyboard=rows)
