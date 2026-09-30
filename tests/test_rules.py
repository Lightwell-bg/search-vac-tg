from pathlib import Path

import pytest

from src.filtering.rules import RuleFilter, _keyword_regex

FILTER = Path(__file__).resolve().parents[1] / "config" / "filter.yaml"


@pytest.fixture(scope="module")
def rf():
    return RuleFilter.from_file(FILTER)


PASS_CASES = {
    "tg_bot": "Нужен разработчик Telegram bot на aiogram для интернет-магазина, бюджет обсуждается",
    "python_automation": "Ищем Python разработчика для автоматизации отчетов, парсинг сайтов и выгрузка в таблицы",
    "n8n": "Требуется специалист по n8n: собрать воркфлоу для заявок из формы в CRM",
    "openai": "Нужно подключить OpenAI API к нашему сайту, ассистент отвечает клиентам",
    "wordpress": "Требуется доработать сайт на WordPress: правки плагина и вёрстка",
    "api": "Нужна интеграция сервиса доставки с нашей системой через API, задача на неделю",
}


@pytest.mark.parametrize("name", list(PASS_CASES))
def test_technical_vacancies_pass(rf, name):
    r = rf.evaluate(PASS_CASES[name])
    assert r.verdict == "pass", r.reason


REJECT_CASES = {
    "smm": "Ищем SMM менеджер для ведения соцсетей нашего салона красоты, оплата достойная",
    "illustrator": "Нужен иллюстратор для детской книги, рисуем персонажей, оплата за работу",
    "designer_logo": "Нужен дизайнер для создания логотипа компании, срочно, оплата по договорённости",
    "copywriter": "Ищем копирайтер для написания текстов на сайт, оплата за 1000 знаков",
}


@pytest.mark.parametrize("name", list(REJECT_CASES))
def test_non_technical_rejected(rf, name):
    r = rf.evaluate(REJECT_CASES[name])
    assert r.verdict == "reject"
    assert "negative" in r.reason


def test_mixed_design_agency_n8n_passes(rf):
    r = rf.evaluate("Нужно автоматизировать работу дизайн-агентства через n8n: заявки, задачи, отчёты")
    assert r.verdict == "pass"
    assert "n8n" in r.technologies


def test_mixed_smm_plus_bot_passes_to_jev(rf):
    r = rf.evaluate("Нужен SMM-менеджер, плюсом умение настроить телеграм-бота для клиентов")
    assert r.verdict == "pass"
    assert r.reason == "needs_semantic_check"
    assert not r.strong_accept  # negatives present -> semantic check by JEV


def test_too_short_rejected(rf):
    r = rf.evaluate("Нужен python бот")
    assert r.verdict == "reject" and r.reason == "too_short"


def test_ad_without_job_signal_rejected(rf):
    r = rf.evaluate("Подписывайтесь на наш канал, здесь мы делимся новостями и полезными советами каждый день")
    assert r.verdict == "reject" and r.reason == "no_job_signal"


def test_job_marker_alone_passes(rf):
    r = rf.evaluate("Ищем человека на постоянную работу в наш офис, обязательно приходить к девяти утра")
    assert r.verdict == "pass"


def test_strong_accept_flag(rf):
    r = rf.evaluate("Нужен Python разработчик: telegram bot на aiogram и n8n автоматизация процессов")
    assert r.verdict == "pass"
    assert r.strong_accept is True
    assert r.reason == "strong_positive"


def test_strong_accept_false_with_single_strong(rf):
    r = rf.evaluate("Нужен разработчик на Python для доработки внутреннего скрипта нашей компании")
    assert r.verdict == "pass"
    assert r.strong_accept is False


def test_strong_accept_false_when_negative_present(rf):
    r = rf.evaluate("Нужен дизайнер и Python разработчик: telegram bot на aiogram и n8n автоматизация")
    assert r.verdict == "pass"
    assert r.strong_accept is False


def test_score_range(rf):
    r = rf.evaluate(PASS_CASES["tg_bot"])
    assert 0 <= r.score <= 100


def test_api_not_matched_in_capital(rf):
    r = rf.evaluate("Ищем аналитика по capital markets и инвестициям для нашей команды в офисе")
    assert "api" not in r.hits.get("positive", [])


def test_api_matched_standalone_and_plural():
    rx = _keyword_regex("api")
    assert rx.search("нужно api для магазина")
    assert rx.search("несколько apis")
    assert not rx.search("capital")
    assert not rx.search("rapid")
    assert not rx.search("apikey")


def test_cyrillic_keyword_takes_ending():
    rx = _keyword_regex("бот")
    assert rx.search("нужен бот")
    assert rx.search("написать бота для магазина")
    assert rx.search("ботов много")
    assert not rx.search("работа")  # left word boundary


def test_multiword_keyword_flexible_separators():
    rx = _keyword_regex("telegram bot")
    assert rx.search("telegram-bot")
    assert rx.search("telegram   bot")
    assert rx.search("Telegram bots")


def test_prefix_star():
    rx = _keyword_regex("автоматиз*")
    assert rx.search("автоматизировать")


def test_bot_in_rules_hits(rf):
    r = rf.evaluate("Нужно написать бота для приёма заявок в нашей компании, срочно")
    assert "бот" in r.hits.get("positive", [])


def test_ignore_contacts_loaded(rf):
    assert isinstance(rf.ignore_contacts, set)
