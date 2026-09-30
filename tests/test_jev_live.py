"""Real JEV calls through OpenRouter (~$0.0001 each). Run: RUN_LIVE_JEV=1 pytest -q -m live"""
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from src.jev.classifier import JevClassifier
from src.jev.client import JevClient
from src.profile.loader import PROVISIONAL_PROFILE, compact_profile

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_JEV") != "1" or not os.getenv("OPENROUTER_API_KEY"),
        reason="set RUN_LIVE_JEV=1 and OPENROUTER_API_KEY to run live JEV tests",
    ),
]

BOT_JOB = ("Нужен разработчик Telegram-бота на Python (aiogram): приём заявок от клиентов, "
           "интеграция с CRM и Google Sheets, деплой на VPS. Бюджет 60 000 руб.")
SMM_JOB = ("Ищем SMM-менеджера для ведения Инстаграма салона красоты: контент-план, сторис, "
           "рилс, оформление профиля. Оплата 40 000 руб в месяц.")


@pytest.fixture
async def classifier():
    client = JevClient("https://openrouter.ai/api/v1/systemone", os.environ["OPENROUTER_API_KEY"],
                       "~typesafe/jev-latest", timeout=30)
    yield JevClassifier(client)
    await client.close()


async def test_live_bot_job_accept(classifier):
    d = await classifier.classify(BOT_JOB, compact_profile(PROVISIONAL_PROFILE), ["telegram bot", "aiogram"])
    print("LIVE bot:", d.decision, d.confidence, d.fit_score, d.category)
    assert d.decision == "accept"


async def test_live_smm_job_reject(classifier):
    d = await classifier.classify(SMM_JOB, compact_profile(PROVISIONAL_PROFILE), [])
    print("LIVE smm:", d.decision, d.confidence, d.fit_score, d.category)
    assert d.decision == "reject"
