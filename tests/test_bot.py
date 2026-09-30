from types import SimpleNamespace

from src.bot.cards import SAFE_LIMIT, build_card, contact_line
from src.bot.handlers import is_owner, parse_callback
from src.bot.keyboards import contact_url, job_keyboard
from src.bot.stats_format import format_stats

SETTINGS = SimpleNamespace(high_fit_score=80, owner_telegram_id=42)


def make_job(**kw):
    base = dict(
        id=7, title="Нужен бот", normalized_text="Нужен бот на aiogram", budget="50 000 руб",
        fit_score=88, route="JEV", decision_reason="fits", contact_status="direct",
        contact_value="@client_name", rules_result={"technologies": ["aiogram"]}, llm_result=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def sources(*names):
    return [SimpleNamespace(channel_username=n) for n in names]


def buttons(kb):
    return [b for row in kb.inline_keyboard for b in row]


def test_html_is_escaped_everywhere():
    job = make_job(
        title="<b>x</b>", normalized_text="<script>alert(1)</script> & more",
        budget="<i>1</i>", contact_value="a<b>@x.com", decision_reason="<u>r</u>",
        rules_result={"relevant_skills": ["<img src=x>"]},
    )
    card = build_card(job, None, sources("Chan<nel>"), SETTINGS, html=True)
    assert "<script>" not in card and "&lt;script&gt;" in card
    assert "<img" not in card and "<i>" not in card and "<u>" not in card
    assert "Chan&lt;nel&gt;" in card and "&amp; more" in card
    assert "<b>&lt;b&gt;x&lt;/b&gt;</b>" in card  # our own bold tag around the escaped title


def test_plain_mode_has_no_markup():
    card = build_card(make_job(), None, sources("FreelanceBay"), SETTINGS, html=False)
    assert "<b>" not in card and "FreelanceBay" in card


def test_header_high_vs_normal():
    high = build_card(make_job(fit_score=80), None, [], SETTINGS)
    normal = build_card(make_job(fit_score=72), None, [], SETTINGS)
    assert high.startswith("🔥 Подходящий заказ — 80/100")
    assert normal.startswith("✅ Возможно подходит — 72/100")


def test_card_is_capped_and_budget_omitted():
    job = make_job(normalized_text="<" * 5000, budget=None)
    card = build_card(job, None, [], SETTINGS)
    assert len(card) <= SAFE_LIMIT
    assert "💰" not in card


def test_contact_lines():
    assert contact_line(make_job(contact_status="paid_contact", contact_value=None)) == "⚠️ платный контакт"
    assert contact_line(make_job(contact_status="contact_unknown", contact_value=None)) == "контакт не найден"
    assert contact_line(make_job(contact_status="external_contact_flow", contact_value="https://t.me/bot?start=1")
                        ).startswith("контакт через бота")
    assert contact_line(make_job()) == "@client_name"


def test_contact_button_variants():
    url = "https://t.me/post/1"
    b = buttons(job_keyboard(7, url, "@client_name"))
    contact = [x for x in b if x.text == "👤 Контакт"][0]
    assert contact.url == "https://t.me/client_name"
    assert [x for x in buttons(job_keyboard(7, url, "https://site.io/x")) if x.text == "👤 Контакт"][0].url \
        == "https://site.io/x"
    for value in ("a@b.com", "+79991234567"):
        c = [x for x in buttons(job_keyboard(7, url, value)) if x.text == "👤 Контакт"][0]
        assert c.url is None and c.callback_data == "c:7"
    assert not [x for x in buttons(job_keyboard(7, url, None)) if x.text == "👤 Контакт"]
    assert contact_url("t.me/name") == "https://t.me/name"


def test_callback_data_short_and_parseable():
    kb = job_keyboard(2_000_000_000, "https://t.me/c/1", "a@b.com", chosen="up")
    datas = [b.callback_data for b in buttons(kb) if b.callback_data]
    assert datas and all(len(d.encode()) < 64 for d in datas)
    assert parse_callback("f:up:5") == ("f", "up", 5)
    assert parse_callback("a:9") == ("a", None, 9)
    assert parse_callback("c:1") == ("c", None, 1)
    for bad in ("f:sideways:5", "a:x", "zzz", "", None, "f:up"):
        assert parse_callback(bad) is None


def test_feedback_button_is_marked():
    texts = [b.text for b in buttons(job_keyboard(1, None, None, chosen="down"))]
    assert "✅ 👎 Мимо" in texts and "👍 Подходит" in texts
    assert "✍️ Сделать отклик" in texts


def test_owner_only():
    assert is_owner(42, 42)
    assert not is_owner(42, 43)
    assert not is_owner(None, 42)
    assert not is_owner(42, None)


def test_format_stats_savings_line():
    text = format_stats({"jev_processed": 10, "openrouter_review_calls": 2, "openrouter_calls": 3,
                         "jobs_by_status": {"notified": 4}})
    assert "OpenRouter вызван для 2 из 10 прошедших правила (80% сэкономлено)" in text
    assert "review 2 / other 1" in text and "notified: 4" in text
    assert "Estimated JEV cost" in format_stats({})


def test_skills_dedupe_case_insensitive_and_skip_generic():
    from src.bot.cards import relevant_skills

    job = make_job(rules_result={"relevant_skills": ["Python", "python", "API", "бот", "n8n"]})
    assert relevant_skills(job) == ["Python", "n8n"]
    # fewer than two specific skills: never a lone generic word, category label is added
    job = make_job(rules_result={"relevant_skills": ["api", "Python", "бот"]}, category="wordpress")
    assert relevant_skills(job) == ["Python", "WordPress"]
    job = make_job(rules_result={"relevant_skills": ["бот"]}, category="telegram_automation")
    assert relevant_skills(job) == ["Telegram-боты и автоматизация"]
    job = make_job(rules_result={"relevant_skills": ["бот"]}, llm_result={"relevant_skills": ["aiogram"]},
                   category="ai_llm")
    assert relevant_skills(job) == ["aiogram", "AI/LLM"]


async def test_application_send_ok_but_bookkeeping_fails_no_failed_message():
    from src.bot.handlers import BotHandlers

    sent: list[str] = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kw):
            sent.append(text)
            return SimpleNamespace(message_id=55)

    class FakeRepo:
        async def save_notification(self, *a, **k):
            raise RuntimeError("db locked")

        async def log_llm_usage(self, **k):
            raise RuntimeError("db locked")

    class FakeLLM:
        model = "m"

        async def write_application(self, profile, job, projects):
            return "Здравствуйте! Готов помочь.", SimpleNamespace(
                model="m", input_tokens=1, output_tokens=1, cost_usd=0.0, duration_ms=1)

    h = BotHandlers(SETTINGS, FakeRepo(), FakeLLM(), {"provisional": True})
    job = make_job()
    h._generating.add(job.id)
    await h._generate(FakeBot(), 42, None, job)
    assert len(sent) == 1 and "Готов помочь" in sent[0]
    assert not any("Не удалось" in t for t in sent)
    assert job.id not in h._generating


TELEJOBO_TEXT = """💬 Разработчик Telegram чатбота для сервиса аренды байков

Что должен уметь бот:
- брать оплату
Писать: @aziza_design
📣 Разместить вакансию / рекламу
⚡ Получать больше вакансий быстрее: «Работодром PRO»
https://t.me/RD_vacbot?start=uttelejobo_vac
https://t.me/RD_adsbot?start=uttelejobo_ads
https://t.me/rabotodrombot?start=shield_telejobo"""


def telejobo_job():
    return make_job(
        title="Разработчик Telegram чатбота для сервиса аренды байков", normalized_text=TELEJOBO_TEXT,
        fit_score=99, budget=None, category="telegram_automation",
        rules_result={"relevant_skills": ["бот"]},
    )


def test_telejobo_card_plain_and_html():
    plain = build_card(telejobo_job(), None, sources("telejobo"), SETTINGS, html=False)
    assert "t.me" not in plain and "Разместить" not in plain and "Получать больше" not in plain
    assert "Писать: @aziza_design" in plain
    assert plain.count("Разработчик Telegram чатбота для сервиса аренды байков") == 1  # 💬 duplicate of the title is dropped
    assert "\n\nПочему подходит:\n• Telegram-боты и автоматизация\n\n📡 telejobo" in plain
    assert "• бот" not in plain
    html = build_card(telejobo_job(), None, sources("telejobo"), SETTINGS, html=True)
    assert "<b>Разработчик Telegram чатбота для сервиса аренды байков</b>" in html
    assert "t.me" not in html


def test_strip_footer_and_title_helpers():
    from src.telegram.parser import drop_title_line, strip_footer

    assert strip_footer("Текст\nреклама: @x\nt.me/foo_bot") == "Текст"
    assert strip_footer("Текст\nt.me/foo_bot\nещё текст") == "Текст\nt.me/foo_bot\nещё текст"
    assert strip_footer("A\nспецмаркер здесь", ["СпецМаркер"]) == "A"
    assert drop_title_line("🔥  Нужен  БОТ\nтело", "нужен бот") == "тело"
    assert drop_title_line("другое\nНужен бот", "Нужен бот") == "другое\nНужен бот"
