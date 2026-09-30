from sqlalchemy import func, select

from src.db.models import Job, Message
from src.filtering.deduplicator import Deduplicator, best_fuzzy_match
from src.telegram.parser import normalize_post

from .helpers import FakeJevClient, build_pipeline, make_post

LONG = ("Нужен разработчик Telegram bot на aiogram для интернет магазина одежды приём заявок "
        "интеграция с CRM и Google Sheets выгрузка отчётов еженедельно поддержка после запуска")


# ------------------------------------------------------------------ best_fuzzy_match


def test_fuzzy_slightly_changed_repost_matches():
    other = LONG
    changed = LONG.replace("еженедельно", "ежедневно") + " срочно"
    m = best_fuzzy_match(changed, [(7, other)], 90)
    assert m is not None and m.job_id == 7 and m.kind == "fuzzy" and m.similarity >= 90


def test_fuzzy_different_vacancies_not_merged():
    a = LONG
    b = ("Требуется мастер по ремонту квартир под ключ, опыт от пяти лет, свой инструмент, "
         "выезд по городу, оплата сдельная, график свободный")
    assert best_fuzzy_match(a, [(1, b)], 90) is None


def test_fuzzy_short_texts_not_matched():
    assert best_fuzzy_match("нужен бот", [(1, "нужен бот")], 90) is None
    assert best_fuzzy_match(LONG, [(1, "нужен бот")], 90) is None


def test_fuzzy_length_ratio_guard():
    short = LONG[:70]
    assert best_fuzzy_match(short, [(1, LONG * 2)], 90) is None


def test_fuzzy_picks_best_candidate():
    close = LONG + " спасибо"
    closer = LONG + " ок"
    m = best_fuzzy_match(LONG, [(1, close), (2, closer), (3, "нечто совсем другое " * 6)], 80)
    assert m is not None and m.job_id in (1, 2)


# ------------------------------------------------------------------ Deduplicator + repo


async def _add_job(repo, post):
    norm = normalize_post(post)
    msg_id, _ = await repo.save_message(
        channel_tg_id=post.channel_tg_id, channel_username=post.channel_username,
        message_id=post.message_id, url=post.url, posted_at=None, original_text=post.text,
        normalized_text=norm.text, buttons=[], extracted={})
    job_id = await repo.create_job(msg_id, norm.text_hash, norm.text, norm.title, norm.budget,
                                   dedup_key=norm.dedup_key)
    return job_id, norm


async def test_exact_duplicate_by_hash(repo):
    job_id, norm = await _add_job(repo, make_post(LONG))
    m = await Deduplicator(repo).find(norm.text_hash, norm.dedup_key)
    assert m is not None and m.job_id == job_id and m.kind == "hash" and m.similarity == 100.0


async def test_normalized_duplicate_same_hash(repo):
    a = make_post("🔥 " + LONG + " @user_one https://t.me/user_one", 1)
    b = make_post(LONG.upper().replace("  ", " ") + "\n\n\n@other_person   www.example.com/x", 2, 200, "chan_two")
    job_id, na = await _add_job(repo, a)
    nb = normalize_post(b)
    assert na.dedup_key == nb.dedup_key and na.text_hash == nb.text_hash
    m = await Deduplicator(repo).find(nb.text_hash, nb.dedup_key)
    assert m.job_id == job_id and m.kind == "hash"


async def test_changed_repost_fuzzy(repo):
    job_id, _ = await _add_job(repo, make_post(LONG))
    changed = normalize_post(make_post(LONG.replace("еженедельно", "ежедневно") + " срочно", 2))
    m = await Deduplicator(repo, 90).find(changed.text_hash, changed.dedup_key)
    assert m is not None and m.job_id == job_id and m.kind == "fuzzy"


async def test_different_vacancy_not_matched(repo):
    await _add_job(repo, make_post(LONG))
    other = normalize_post(make_post(
        "Требуется мастер по ремонту квартир под ключ, опыт от пяти лет, свой инструмент, "
        "выезд по городу, оплата сдельная, график свободный", 2))
    assert await Deduplicator(repo).find(other.text_hash, other.dedup_key) is None


async def test_short_text_hash_only(repo):
    job_id, _ = await _add_job(repo, make_post("Нужен бот, пишите @client_user срочно"))
    near = normalize_post(make_post("Нужен бот, пишите @client_user срочно!", 2))
    # same key -> hash match; different key + short -> no fuzzy
    assert (await Deduplicator(repo).find(near.text_hash, near.dedup_key)).kind == "hash"
    other = normalize_post(make_post("Нужен бот, пишите срочно всем", 3))
    assert await Deduplicator(repo).find(other.text_hash, other.dedup_key) is None


# ------------------------------------------------------------------ through the pipeline


async def _count(repo, model):
    async with repo.db.session() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def test_one_vacancy_two_channels_single_job_single_jev_call(repo):
    from .helpers import TECH_TEXT
    pipe, jev, llm, actions, notifier = build_pipeline(repo)
    r1 = await pipe.process_post(make_post("🔥 " + TECH_TEXT, 1, 100, "chan_one"))
    r2 = await pipe.process_post(make_post(TECH_TEXT.replace("Пишите", "  пишите  ") + " ✅", 5, 200, "chan_two"))
    assert r1 == "notified" and r2 == "duplicate"
    assert len(jev.calls) == 1
    assert await _count(repo, Job) == 1
    assert await _count(repo, Message) == 2
    sources = await repo.get_job_sources(1)
    assert len(sources) == 2
    assert {s.channel_username for s in sources} == {"chan_one", "chan_two"}
    assert [s.match_kind for s in sources] == ["original", "hash"]
    async with repo.db.session() as s:
        dup = (await s.execute(select(Message).where(Message.message_id == 5))).scalar_one()
    assert dup.is_duplicate is True and dup.job_id == 1
    assert len(notifier.calls) == 1


async def test_fuzzy_repost_through_pipeline(repo):
    pipe, jev, *_ = build_pipeline(repo)
    await pipe.process_post(make_post(LONG + " Пишите @client_user", 1))
    r = await pipe.process_post(make_post(LONG.replace("еженедельно", "ежедневно") + " Пишите @client_user", 2, 200, "chan_two"))
    assert r == "duplicate"
    sources = await repo.get_job_sources(1)
    assert [s.match_kind for s in sources] == ["original", "fuzzy"]
    assert sources[1].similarity >= 90
    assert len(jev.calls) == 1


async def test_different_vacancies_not_merged_in_pipeline(repo):
    from .helpers import TECH_TEXT
    pipe, jev, *_ = build_pipeline(repo)
    await pipe.process_post(make_post(TECH_TEXT, 1))
    r = await pipe.process_post(make_post(
        "Ищем Python разработчика для парсинга маркетплейсов и выгрузки цен в Google Sheets, "
        "нужен опыт scraping и docker. Пишите @client_two", 2, 200, "chan_two"))
    assert r == "notified"
    assert await _count(repo, Job) == 2
    assert len(jev.calls) == 2


def test_short_identical_ads_with_different_contacts_have_different_hash():
    a = normalize_post(make_post("Нужен бот, пишите @client_one"))
    b = normalize_post(make_post("Нужен бот, пишите @client_two", 2))
    assert a.text_hash != b.text_hash


def test_short_ad_same_contact_same_hash_and_long_ads_ignore_contacts():
    a = normalize_post(make_post("Нужен бот, пишите @client_one"))
    b = normalize_post(make_post("Нужен БОТ пишите @Client_One!", 2))
    assert a.text_hash == b.text_hash
    la = normalize_post(make_post(LONG + " @one_user"))
    lb = normalize_post(make_post(LONG + " @two_user", 2))
    assert la.text_hash == lb.text_hash
