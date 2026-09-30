import json

import httpx
import pytest
import respx

from src.llm.openrouter import OpenRouterClient, parse_json_object
from src.llm.schemas import LlmError

BASE = "https://or.test/api/v1"
URL = BASE + "/chat/completions"

GOOD = {"fit_score": 85, "category": "telegram_bot", "relevant_skills": ["Python", "aiogram"],
        "missing_skills": [], "reason": "Подходит", "should_notify": True}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr("asyncio.sleep", _noop)


def completion(content, model="vendor/model-x"):
    return httpx.Response(200, json={
        "model": model,
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 40, "cost": 0.0021},
    })


def make(key="k", model="my/model", retries=2) -> OpenRouterClient:
    return OpenRouterClient(key, model, BASE, timeout=5, max_retries=retries)


@respx.mock
async def test_review_happy_path():
    route = respx.post(URL).mock(return_value=completion(json.dumps(GOOD)))
    res, usage = await make().review_job("profile", "job text", "jev line")
    assert res.fit_score == 85 and res.should_notify and res.relevant_skills == ["Python", "aiogram"]
    assert usage.input_tokens == 120 and usage.output_tokens == 40 and usage.cost_usd == 0.0021
    assert usage.model == "vendor/model-x"
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "my/model"
    assert body["response_format"] == {"type": "json_object"}
    assert "job text" in body["messages"][1]["content"] and "jev line" in body["messages"][1]["content"]
    assert route.calls[0].request.headers["authorization"] == "Bearer k"


@respx.mock
async def test_fenced_json_parsed():
    respx.post(URL).mock(return_value=completion("```json\n" + json.dumps(GOOD) + "\n```"))
    res, _ = await make().review_job("p", "j", "s")
    assert res.fit_score == 85


@respx.mock
async def test_malformed_json_invalid_with_usage():
    respx.post(URL).mock(return_value=completion("sorry, I cannot do that"))
    with pytest.raises(LlmError) as ei:
        await make().review_job("p", "j", "s")
    assert ei.value.kind == "invalid_json"
    assert ei.value.usage is not None and ei.value.usage.input_tokens == 120


@respx.mock
async def test_schema_violation_invalid_json_with_usage():
    respx.post(URL).mock(return_value=completion(json.dumps({**GOOD, "fit_score": "abc"})))
    with pytest.raises(LlmError) as ei:
        await make().review_job("p", "j", "s")
    assert ei.value.kind == "invalid_json"
    assert ei.value.usage is not None and ei.value.usage.cost_usd == 0.0021


@respx.mock
async def test_fit_score_clamped():
    respx.post(URL).mock(return_value=completion(json.dumps({**GOOD, "fit_score": 250})))
    res, _ = await make().review_job("p", "j", "s")
    assert res.fit_score == 100


@respx.mock
async def test_429_then_success():
    route = respx.post(URL).mock(side_effect=[httpx.Response(429, text="slow down"),
                                              completion(json.dumps(GOOD))])
    res, _ = await make().review_job("p", "j", "s")
    assert res.fit_score == 85 and route.call_count == 2


@respx.mock
async def test_timeout_exhausts_retries():
    route = respx.post(URL).mock(side_effect=httpx.ReadTimeout("t"))
    with pytest.raises(LlmError) as ei:
        await make(retries=2).review_job("p", "j", "s")
    assert ei.value.kind == "timeout" and route.call_count == 3
    assert ei.value.usage is not None


@respx.mock
async def test_4xx_not_retried():
    route = respx.post(URL).mock(return_value=httpx.Response(401, text="no"))
    with pytest.raises(LlmError) as ei:
        await make().review_job("p", "j", "s")
    assert ei.value.kind == "http" and route.call_count == 1


@respx.mock
async def test_500_retried_then_http_error():
    route = respx.post(URL).mock(return_value=httpx.Response(503, text="x"))
    with pytest.raises(LlmError) as ei:
        await make(retries=1).review_job("p", "j", "s")
    assert ei.value.kind == "http" and route.call_count == 2


@respx.mock
async def test_bad_payload_shape_invalid_json():
    respx.post(URL).mock(return_value=httpx.Response(200, json={"choices": []}))
    with pytest.raises(LlmError) as ei:
        await make().review_job("p", "j", "s")
    assert ei.value.kind == "invalid_json"


@pytest.mark.parametrize("key,model", [("", "m"), ("k", "")])
async def test_missing_key_or_model_config(key, model):
    with respx.mock(assert_all_called=False) as m:
        route = m.post(URL).mock(return_value=completion("{}"))
        with pytest.raises(LlmError) as ei:
            await OpenRouterClient(key, model, BASE).review_job("p", "j", "s")
        assert ei.value.kind == "config" and route.call_count == 0


@respx.mock
async def test_write_application_returns_text_and_no_json_mode():
    route = respx.post(URL).mock(return_value=completion("  Здравствуйте! Готов помочь.  "))
    text, usage = await make().write_application("profile", "job", [{"name": "P", "summary": "s", "stack": ["Python"]}])
    assert text == "Здравствуйте! Готов помочь."
    assert usage.output_tokens == 40
    body = json.loads(route.calls[0].request.content)
    assert "response_format" not in body
    assert "P: s [Python]" in body["messages"][1]["content"]


# ------------------------------------------------------------------ parse_json_object


def test_parse_plain():
    assert parse_json_object('{"a": 1}') == {"a": 1}


def test_parse_fenced_variants():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('```\n{"a": 1}\n```') == {"a": 1}


def test_parse_surrounded_by_prose():
    assert parse_json_object('Here you go: {"a": {"b": 2}} hope it helps') == {"a": {"b": 2}}


@pytest.mark.parametrize("bad", ["", "no json here", "[1, 2]", "{broken", '{"a": }'])
def test_parse_errors(bad):
    with pytest.raises(LlmError) as ei:
        parse_json_object(bad)
    assert ei.value.kind == "invalid_json"


# ------------------------------------------------------------------ malformed bodies


@respx.mock
async def test_non_dict_json_body_is_llm_error():
    respx.post(URL).mock(return_value=httpx.Response(200, json=["x"]))
    with pytest.raises(LlmError) as ei:
        await make().review_job("p", "j", "s")
    assert ei.value.kind == "invalid_json"


@respx.mock
async def test_non_dict_usage_gives_zero_usage():
    respx.post(URL).mock(return_value=httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps(GOOD)}}], "usage": "lots"}))
    res, usage = await make().review_job("p", "j", "s")
    assert res.fit_score == 85
    assert (usage.input_tokens, usage.output_tokens, usage.cost_usd) == (0, 0, 0.0)
    assert usage.model == "my/model"


@respx.mock
async def test_non_numeric_usage_fields_are_zero():
    respx.post(URL).mock(return_value=httpx.Response(200, json={
        "model": ["x"],
        "choices": [{"message": {"content": json.dumps(GOOD)}}],
        "usage": {"prompt_tokens": "12k", "completion_tokens": [1], "cost": {"usd": 1}}}))
    res, usage = await make().review_job("p", "j", "s")
    assert res.fit_score == 85
    assert (usage.input_tokens, usage.output_tokens, usage.cost_usd) == (0, 0, 0.0)
    assert usage.model == "my/model"


@respx.mock
async def test_non_text_content_is_llm_error():
    respx.post(URL).mock(return_value=httpx.Response(200, json={
        "choices": [{"message": {"content": [{"type": "text"}]}}]}))
    with pytest.raises(LlmError) as ei:
        await make().review_job("p", "j", "s")
    assert ei.value.kind == "invalid_json"
