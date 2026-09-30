"""Run the REAL pipeline on sample texts without Telegram.

Usage: python scripts/dry_run.py [--file texts.txt]
Texts in the file are separated by lines containing only ``---``.
Uses a throw-away SQLite db (data/dry_run.db), the real JEV and, if OPENROUTER_MODEL is set,
the real OpenRouter. Contact buttons are never clicked (no Telegram actions).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _stream in (sys.stdout, sys.stderr):  # Windows cp1251 console cannot print "→"
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import argparse  # noqa: E402
import asyncio  # noqa: E402
import dataclasses  # noqa: E402
import logging  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from sqlalchemy import select  # noqa: E402

from src.bot.cards import build_card  # noqa: E402
from src.bot.stats_format import format_stats  # noqa: E402
from src.config import PROJECT_ROOT, load_settings  # noqa: E402
from src.db.database import Database  # noqa: E402
from src.db.models import Message  # noqa: E402
from src.db.repository import Repository  # noqa: E402
from src.main import build_pipeline  # noqa: E402
from src.telegram.parser import ButtonInfo, RawPost  # noqa: E402

CHANNEL = "dry_run"

SAMPLES = [
    "Нужен Telegram-бот на aiogram: приём заявок от клиентов, интеграция с OpenAI API для ответов на вопросы "
    "и выгрузка лидов в CRM (amoCRM). Бюджет 60 000 руб. Сроки — 2 недели. Пишите @dry_client",
    "Ищем специалиста по n8n: автоматизировать процессы дизайн-агентства — новые заявки из Telegram в Google "
    "Sheets, уведомления менеджерам, генерация счетов. Бюджет обсуждается.",
    "Требуется SMM-менеджер для ведения Instagram и Telegram-канала интернет-магазина: контент-план, "
    "посты, сторис, работа с подрядчиками. Оплата 40 000 руб/мес.",
    "Нужен дизайнер для создания логотипа и фирменного стиля кофейни: 3 варианта, исходники в AI и PNG. "
    "Бюджет 15 000 руб.",
    "Срочно: на сайте WordPress + WooCommerce сломался плагин доставки — не считается стоимость по "
    "весу. Нужно починить и проверить корзину. Оплата 8000 руб.",
    "Нужен помощник для проекта, детали в лс. Пишите, обсудим формат и условия сотрудничества.",
    "Нужен парсер на Python для маркетплейса: собирать цены и остатки по списку артикулов раз в сутки, "
    "выгрузка в Google Sheets, Docker на VPS. Бюджет 30 000 руб.",
    "Ищем копирайтера для ведения корпоративного блога: 8 статей в месяц по 3000 знаков, SEO-оптимизация, "
    "оплата 500 руб за статью.",
]


def sample_posts(texts: list[str], with_buttons: bool = True) -> list[RawPost]:
    now = datetime.now(timezone.utc)
    posts = []
    for i, text in enumerate(texts, start=1):
        buttons: list[ButtonInfo] = []
        if not with_buttons:
            pass
        elif i == 2:
            buttons = [ButtonInfo("Получить контакт", "callback", data_hex="0a0b")]
        elif i == 7:
            buttons = [ButtonInfo("Написать заказчику", "url", url="https://t.me/some_client")]
        posts.append(RawPost(channel_tg_id=1, channel_username=CHANNEL, message_id=1000 + i, date=now,
                             text=text, buttons=buttons))
    return posts


def load_texts(path: str | None) -> list[str]:
    if not path:
        return list(SAMPLES)
    raw = Path(path).read_text(encoding="utf-8-sig")
    chunks, cur = [], []
    for line in raw.splitlines():
        if line.strip() == "---":
            chunks.append("\n".join(cur).strip())
            cur = []
        else:
            cur.append(line)
    chunks.append("\n".join(cur).strip())
    return [c for c in chunks if c]


class PrintNotifier:
    """Prints the card (plain text) instead of sending it to Telegram."""

    def __init__(self, repo: Repository, settings) -> None:
        self.repo = repo
        self.settings = settings
        self.cards: dict[int, str] = {}

    async def notify_job(self, job_id: int) -> tuple[int, int | None]:
        job = await self.repo.get_job(job_id)
        message = await self.repo.get_primary_message(job_id)
        sources = await self.repo.get_job_sources(job_id)
        self.cards[job_id] = build_card(job, message, sources, self.settings, html=False)
        return 0, None


def _cut(s: object, n: int) -> str:
    s = "" if s is None else str(s).replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


async def main(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    base = load_settings()
    db_path = PROJECT_ROOT / "data" / "dry_run.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    settings = dataclasses.replace(base, database_url=f"sqlite+aiosqlite:///{db_path.as_posix()}")
    db = Database(settings.database_url)
    await db.init()
    repo = Repository(db)
    notifier = PrintNotifier(repo, settings)
    pipeline = build_pipeline(settings, repo, telegram_actions=None, notifier=notifier)
    print(f"JEV: openrouter {settings.jev_model}; OpenRouter: "
          f"{settings.openrouter_model or 'not configured'}")
    rows = []
    try:
        for i, post in enumerate(sample_posts(load_texts(args.file), with_buttons=not args.file), start=1):
            outcome = await pipeline.process_post(post)
            async with db.session() as s:
                job_id = (await s.execute(select(Message.job_id).where(
                    Message.channel_tg_id == post.channel_tg_id,
                    Message.message_id == post.message_id))).scalar_one_or_none()
            job = await repo.get_job(job_id) if job_id else None
            rows.append((i, outcome, job.route if job else "", job.fit_score if job else "",
                         job.contact_status if job else "", job.title if job else post.text[:40], job_id))
        header = f"{'#':>2}  {'outcome':<14} {'route':<24} {'fit':>4}  {'contact_status':<22} title"
        print(header)
        print("-" * len(header))
        for i, outcome, route, fit, contact, title, _ in rows:
            print(f"{i:>2}  {_cut(outcome, 14):<14} {_cut(route, 24):<24} {_cut(fit, 4):>4}  "
                  f"{_cut(contact, 22):<22} {_cut(title, 50)}")
        for i, *_rest, job_id in rows:
            if job_id in notifier.cards:
                print(f"\n=== card #{i} ===\n{notifier.cards[job_id]}")
        print("\n" + format_stats(await repo.get_stats()))
    finally:
        if pipeline.llm is not None:
            await pipeline.llm.close()
        await pipeline.jev.client.close()
        await db.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", help="text file with posts separated by lines '---'")
    sys.exit(asyncio.run(main(ap.parse_args())))
