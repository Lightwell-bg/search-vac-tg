"""Shared fakes and builders for the pipeline-level tests (no network)."""
from __future__ import annotations

from pathlib import Path

from src.filtering.deduplicator import Deduplicator
from src.filtering.pipeline import Pipeline, PipelineOptions
from src.filtering.rules import RuleFilter
from src.jev.classifier import JevClassifier
from src.jev.schemas import JevUsage
from src.llm.schemas import LlmUsage, ReviewResult
from src.profile.loader import PROVISIONAL_PROFILE
from src.telegram.contact_resolver import CallbackAnswer, ContactResolver
from src.telegram.parser import ButtonInfo, RawPost

FILTER_FILE = Path(__file__).resolve().parents[1] / "config" / "filter.yaml"

TECH_TEXT = (
    "Нужен разработчик Telegram bot на aiogram: приём заявок, интеграция с CRM и Google Sheets. "
    "Бюджет: 50 000 руб. Пишите @client_user"
)
AMBIGUOUS_TEXT = (
    "Нужен специалист, который поможет наладить процессы в нашей компании: немного таблиц, "
    "немного интеграции между сервисами, возможно бот. Пишите @client_user"
)
DESIGN_TEXT = "Нужен дизайнер для создания логотипа и фирменного стиля кофейни. Пишите @client_user"
SMM_TEXT = "Ищем SMM менеджер для ведения соцсетей, оплата по договорённости. Пишите @client_user"


def jev_answers(decision: str = "accept", confidence: float = 0.95, fit: float = 3.0,
                category: str = "telegram_automation") -> dict:
    """Answers in the real JEV System One format."""
    return {
        "decision": {"type": "choice", "choice": decision, "confidence": confidence,
                     "probabilities": {decision: confidence}},
        "fit": {"type": "score", "score": fit, "confidence": 0.9, "probabilities": {}},
        "category": {"type": "choice", "choice": category, "confidence": 0.9, "probabilities": {}},
    }


class FakeJevClient:
    """Stands in for JevClient. ``behavior`` is an answers dict, an Exception or a callable."""

    model = "fake-jev"

    def __init__(self, behavior=None):
        self.behavior = behavior if behavior is not None else jev_answers()
        self.calls: list[tuple[dict, dict]] = []

    async def ask(self, state, questions):
        self.calls.append((state, questions))
        b = self.behavior
        if callable(b):
            b = b(state)
        if isinstance(b, BaseException):
            raise b
        return b, JevUsage(model=self.model, input_tokens=100, output_tokens=10,
                           cost_usd=0.0001, duration_ms=5)

    async def close(self):
        pass


def review(fit: int = 85, notify: bool = True, **kw) -> ReviewResult:
    return ReviewResult(fit_score=fit, should_notify=notify, category=kw.pop("category", "telegram_bot"),
                        relevant_skills=kw.pop("relevant_skills", ["Python"]),
                        reason=kw.pop("reason", "ok"), **kw)


class FakeLLM:
    """Stands in for OpenRouterClient. ``behavior``: ReviewResult, Exception or callable."""

    model = "fake/llm"

    def __init__(self, behavior=None):
        self.behavior = behavior if behavior is not None else review()
        self.calls = 0
        self.args: list[tuple] = []

    async def review_job(self, profile, job, jev_short):
        self.calls += 1
        self.args.append((profile, job, jev_short))
        b = self.behavior
        if callable(b):
            b = b()
        if isinstance(b, BaseException):
            raise b
        return b, LlmUsage(self.model, 200, 50, 0.001, 30)


class FakeActions:
    """TelegramActions fake: configurable callback answer / refetch result."""

    def __init__(self, answer: CallbackAnswer | Exception | None = None, refetched=None):
        self.answer = answer if answer is not None else CallbackAnswer()
        self.refetched = refetched
        self.clicks = 0
        self.refetches = 0

    async def click_callback(self, post, button):
        self.clicks += 1
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    async def refetch(self, post):
        self.refetches += 1
        if isinstance(self.refetched, BaseException):
            raise self.refetched
        return self.refetched


class FakeNotifier:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls: list[int] = []

    async def notify_job(self, job_id):
        self.calls.append(job_id)
        if self.error is not None:
            raise self.error
        return 1, 100


def make_post(text: str, message_id: int = 1, channel_tg_id: int = 100,
              channel_username: str | None = "chan_one", buttons: list[ButtonInfo] | None = None) -> RawPost:
    return RawPost(channel_tg_id, channel_username, message_id, None, text, buttons or [])


def build_pipeline(repo, *, jev=None, llm=None, actions=None, notifier=None, options=None,
                   llm_none: bool = False, min_confidence: float = 0.7):
    jev = jev if jev is not None else FakeJevClient()
    llm = None if llm_none else (llm if llm is not None else FakeLLM())
    rules = RuleFilter.from_file(FILTER_FILE)
    notifier = notifier if notifier is not None else FakeNotifier()
    actions = actions if actions is not None else FakeActions()
    pipe = Pipeline(
        repo=repo,
        rules=rules,
        dedup=Deduplicator(repo, 90, 14, 1000),
        jev=JevClassifier(jev, min_confidence=min_confidence),
        llm=llm,
        resolver=ContactResolver(actions, rules.ignore_contacts),
        notifier=notifier,
        profile=dict(PROVISIONAL_PROFILE),
        options=options or PipelineOptions(),
    )
    return pipe, jev, llm, actions, notifier
