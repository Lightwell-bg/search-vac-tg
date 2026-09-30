from pathlib import Path

from src.profile.builder import Document, extract_projects, parse_sections
from src.profile.loader import compact_profile
from src.profile.service import CACHE_VERSION, ProfileService

MD = """# Портфолио

## Оглавление
1. Telegram-бот

## 1. 🤖 Telegram-бот для записи клиентов
Бот для записи клиентов в салон. Стек: не указан, текст упоминает aiogram, PostgreSQL, Docker.
Напоминания и оплата через бота, админка для мастера.

## Контакты
Telegram: @someone, email: a@b.c, телефон и прочие способы связи для заказа работы.

## 2. Парсер вакансий
Короткое.

## Проект без технологий
Очень длинное описание проекта, в котором нет ни одной технологии из списка, только слова и слова и слова.
"""


def test_parse_sections_without_stack_line():
    # the "Стек:" line above is inside the body, so it is handled by the old parser; use a variant without it
    md = MD.replace("Стек: не указан, текст", "Текст")
    res = parse_sections(md, "p.md")
    assert [p["name"] for p in res] == ["Telegram-бот для записи клиентов"]
    p = res[0]
    assert {"aiogram", "PostgreSQL", "Docker"} <= set(p["stack"])
    assert p["type"] == "telegram_bot"
    assert p["source"] == "p.md" and len(p["summary"]) <= 300


def test_extract_projects_txt_and_dedup():
    md = MD.replace("Стек: не указан, текст", "Текст")
    docs = [Document(Path("a.md"), md), Document(Path("b.txt"), md)]
    assert len(extract_projects(docs)) == 1


def test_stack_line_wins():
    md = "## Бот — описание\nСтек: aiogram, Redis\n" + "x" * 100
    res = extract_projects([Document(Path("a.md"), md)])
    assert len(res) == 1 and res[0]["stack"] == ["aiogram", "Redis"]


def _long_profile():
    return {
        "summary": "Developer. " * 5,
        "technologies": [f"Technology{i}" for i in range(40)],
        "services": [f"Service number {i}" for i in range(12)],
        "strong_matches": [f"Strong match {i}" for i in range(12)],
        "acceptable_matches": [f"Acceptable match {i}" for i in range(12)],
        "reject_categories": ["design", "SMM", "copywriting", "video editing", "sales", "recruiting",
                              "accounting", "non-technical tasks"],
    }


def test_compact_profile_keeps_not_interested():
    text = compact_profile(_long_profile())
    assert len(text) <= 1400
    lines = text.splitlines()
    assert any(ln.startswith("Not interested: ") and ln.endswith("non-technical tasks") for ln in lines)
    assert not any(ln.rstrip().endswith(",") for ln in lines)
    stack = next(ln for ln in lines if ln.startswith("Stack: "))
    assert stack.count(",") + 1 <= 30


def test_compact_profile_order():
    text = compact_profile(_long_profile())
    keys = [ln.split(":")[0] for ln in text.splitlines()[1:]]
    assert keys == ["Stack", "Does", "Best fit", "Not interested", "Also OK"]


def test_stale_cache_version(tmp_path):
    class S:
        profile_file = tmp_path / "profile.json"

    svc = ProfileService(S())
    svc.uploads_cache.write_text('{"fingerprint": [], "sources": []}', encoding="utf-8")
    assert svc._cache_fresh()[0] is False
    svc.uploads_cache.write_text(f'{{"fingerprint": [], "cache_version": {CACHE_VERSION}}}', encoding="utf-8")
    assert svc._cache_fresh()[0] is True
