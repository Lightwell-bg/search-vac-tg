import json

import httpx
import pytest
import respx

from src.jev.classifier import JevClassifier
from src.jev.client import JevClient
from src.jev.schemas import JevError

from .helpers import jev_answers

URL = "https://jev.test/v1/systemone"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr("asyncio.sleep", _noop)


def ok_response(answers=None):
    return httpx.Response(200, json={
        "model": "~typesafe/jev-latest",
        "answers": answers if answers is not None else jev_answers(),
        "usage": {"input_tokens": 321, "output_tokens": 12, "cost": 0.00042},
    })


def client(key="k") -> JevClient:
    return JevClient(URL, key, "~typesafe/jev-latest", timeout=5)


# ------------------------------------------------------------------ JevClient


@respx.mock
async def test_client_success_parses_answers_and_usage():
    route = respx.post(URL).mock(return_value=ok_response())
    c = client()
    answers, usage = await c.ask({"job": "x"}, {"decision": {}})
    await c.close()
    assert answers["decision"]["choice"] == "accept"
    assert usage.input_tokens == 321 and usage.output_tokens == 12
    assert usage.cost_usd == 0.00042 and usage.model == "~typesafe/jev-latest"
    req = route.calls[0].request
    assert req.headers["authorization"] == "Bearer k"
    body = json.loads(req.content)
    assert body["model"] == "~typesafe/jev-latest" and body["state"] == {"job": "x"}


@respx.mock
async def test_client_timeout():
    respx.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(JevError) as ei:
        await client().ask({}, {})
    assert ei.value.kind == "timeout"


@respx.mock
async def test_client_connect_error_is_http():
    respx.post(URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(JevError) as ei:
        await client().ask({}, {})
    assert ei.value.kind == "http"


@respx.mock
async def test_client_500_twice_http_error_one_retry():
    route = respx.post(URL).mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(JevError) as ei:
        await client().ask({}, {})
    assert ei.value.kind == "http" and "500" in str(ei.value)
    assert route.call_count == 2


@respx.mock
async def test_client_429_then_200():
    route = respx.post(URL).mock(side_effect=[httpx.Response(429, headers={"retry-after": "1"}), ok_response()])
    answers, _ = await client().ask({}, {})
    assert route.call_count == 2 and "decision" in answers


@respx.mock
async def test_client_400_not_retried():
    route = respx.post(URL).mock(return_value=httpx.Response(400, text="bad"))
    with pytest.raises(JevError) as ei:
        await client().ask({}, {})
    assert ei.value.kind == "http" and route.call_count == 1


@respx.mock
async def test_client_non_json_invalid():
    respx.post(URL).mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(JevError) as ei:
        await client().ask({}, {})
    assert ei.value.kind == "invalid"


@respx.mock
async def test_client_missing_answers_invalid():
    respx.post(URL).mock(return_value=httpx.Response(200, json={"usage": {}}))
    with pytest.raises(JevError) as ei:
        await client().ask({}, {})
    assert ei.value.kind == "invalid"


async def test_client_empty_key_config_no_request():
    with respx.mock(assert_all_called=False) as m:
        route = m.post(URL).mock(return_value=ok_response())
        with pytest.raises(JevError) as ei:
            await client(key="").ask({}, {})
        assert ei.value.kind == "config" and route.call_count == 0


# ------------------------------------------------------------------ JevClassifier.parse


def clf(**kw) -> JevClassifier:
    return JevClassifier(client(), **kw)


def test_parse_accept():
    d = clf().parse(jev_answers("accept", 0.95, 3.0))
    assert d.decision == "accept" and d.raw_decision == "accept"
    assert d.fit_score == 100 and d.category == "telegram_automation"


def test_parse_reject():
    d = clf().parse(jev_answers("reject", 0.9, 0.0, "non_tech"))
    assert d.decision == "reject" and d.fit_score == 0 and d.category == "non_tech"


def test_parse_review():
    d = clf().parse(jev_answers("review", 0.5, 1.5))
    assert d.decision == "review"


def test_parse_low_confidence_becomes_review():
    d = clf(min_confidence=0.7).parse(jev_answers("accept", 0.6, 3.0))
    assert d.decision == "review" and d.raw_decision == "accept"
    assert "low confidence" in d.reason
    d = clf(min_confidence=0.7).parse(jev_answers("reject", 0.6, 0.0))
    assert d.decision == "review"


def test_parse_accept_with_low_fit_becomes_review():
    d = clf().parse(jev_answers("accept", 0.95, 1.4))
    assert d.decision == "review" and "low fit" in d.reason
    assert clf().parse(jev_answers("accept", 0.95, 1.5)).decision == "accept"


def test_parse_reject_with_high_fit_becomes_review():
    d = clf().parse(jev_answers("reject", 0.95, 2.0))
    assert d.decision == "review" and "high fit" in d.reason
    assert clf().parse(jev_answers("reject", 0.95, 1.9)).decision == "reject"


def test_parse_invalid_choice():
    a = jev_answers()
    a["decision"]["choice"] = "maybe"
    with pytest.raises(JevError) as ei:
        clf().parse(a)
    assert ei.value.kind == "invalid"


def test_parse_missing_parts_invalid():
    for key in ("decision", "fit"):
        a = jev_answers()
        del a[key]
        with pytest.raises(JevError):
            clf().parse(a)
    a = jev_answers()
    a["fit"]["score"] = "high"
    with pytest.raises(JevError):
        clf().parse(a)


def test_parse_confidence_falls_back_to_probabilities():
    a = jev_answers("accept", 0.0, 3.0)
    del a["decision"]["confidence"]
    a["decision"]["probabilities"] = {"accept": 0.9, "reject": 0.1}
    d = clf().parse(a)
    assert d.confidence == 0.9 and d.decision == "accept"


def test_parse_no_confidence_no_probabilities_invalid():
    a = jev_answers()
    del a["decision"]["confidence"]
    a["decision"]["probabilities"] = {}
    with pytest.raises(JevError):
        clf().parse(a)


def test_fit_score_mapping():
    assert clf().parse(jev_answers(fit=3.0)).fit_score == 100
    assert clf().parse(jev_answers(fit=1.5)).fit_score == 50
    assert clf().parse(jev_answers(fit=0.0)).fit_score == 0
    assert clf().parse(jev_answers(fit=3.0 + 1e-7)).fit_score == 100  # float tolerance only
    for bad in (5.0, -1.0):  # out-of-contract values are errors, never clamped
        with pytest.raises(JevError):
            clf().parse(jev_answers(fit=bad))


def test_unknown_category_is_none():
    a = jev_answers()
    a["category"]["choice"] = "weird"
    assert clf().parse(a).category is None
    del a["category"]
    assert clf().parse(a).category is None


# ------------------------------------------------------------------ request body


@respx.mock
async def test_classify_sends_only_profile_job_keywords_and_truncates():
    route = respx.post(URL).mock(return_value=ok_response())
    c = JevClassifier(client(), max_text_chars=50)
    res = await c.classify("x" * 500, "PROFILE TEXT", ["python", "n8n"])
    body = json.loads(route.calls[0].request.content)
    assert set(body["state"]) == {"profile", "job", "keywords_found"}
    assert body["state"]["profile"] == "PROFILE TEXT"
    assert body["state"]["job"] == "x" * 50
    assert body["state"]["keywords_found"] == "python, n8n"
    assert set(body["questions"]) == {"decision", "fit", "category"}
    assert res.usage.input_tokens == 321


@respx.mock
async def test_classify_without_hits_omits_keywords():
    route = respx.post(URL).mock(return_value=ok_response())
    await JevClassifier(client()).classify("job", "profile", [])
    body = json.loads(route.calls[0].request.content)
    assert set(body["state"]) == {"profile", "job"}


# ------------------------------------------------------------------ malformed shapes (adapter hardening)


@respx.mock
async def test_client_non_dict_json_is_invalid():
    respx.post(URL).mock(return_value=httpx.Response(200, json=["not", "an", "object"]))
    with pytest.raises(JevError) as ei:
        await client().ask({}, {})
    assert ei.value.kind == "invalid"


@respx.mock
async def test_client_non_dict_usage_treated_as_empty():
    respx.post(URL).mock(return_value=httpx.Response(200, json={"answers": jev_answers(), "usage": [1, 2]}))
    answers, usage = await client().ask({}, {})
    assert "decision" in answers
    assert usage.input_tokens is None and usage.cost_usd is None and usage.model == "~typesafe/jev-latest"


@respx.mock
async def test_client_non_numeric_usage_fields_ignored():
    respx.post(URL).mock(return_value=httpx.Response(
        200, json={"answers": jev_answers(), "usage": {"input_tokens": "many", "cost": {"x": 1}}}))
    _, usage = await client().ask({}, {})
    assert usage.input_tokens is None and usage.cost_usd is None


@respx.mock
async def test_client_http_date_retry_after_does_not_crash():
    route = respx.post(URL).mock(side_effect=[
        httpx.Response(429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}), ok_response()])
    answers, _ = await client().ask({}, {})
    assert len(route.calls) == 2 and "decision" in answers


def test_parse_category_as_list_is_none():
    a = jev_answers()
    a["category"] = ["telegram_automation"]
    assert clf().parse(a).category is None
    a["category"] = {"choice": ["x"]}
    assert clf().parse(a).category is None


def test_parse_probabilities_as_string_falls_back_to_confidence():
    a = jev_answers("accept", 0.9, 3.0)
    a["decision"]["probabilities"] = "accept=0.9"
    d = clf().parse(a)
    assert d.probabilities == {} and d.confidence == 0.9


def test_parse_probabilities_string_and_no_confidence_invalid():
    a = jev_answers()
    del a["decision"]["confidence"]
    a["decision"]["probabilities"] = "accept=0.9"
    with pytest.raises(JevError):
        clf().parse(a)


@pytest.mark.parametrize("conf", [float("nan"), float("inf"), 1.5, -0.1, "0.9", True])
def test_parse_bad_confidence_without_probabilities_invalid(conf):
    a = jev_answers()
    a["decision"]["confidence"] = conf
    a["decision"]["probabilities"] = {}
    with pytest.raises(JevError):
        clf().parse(a)


def test_parse_bad_confidence_uses_valid_probability():
    a = jev_answers("accept", 1.5, 3.0)
    a["decision"]["probabilities"] = {"accept": 0.92, "reject": float("nan"), "review": 7}
    d = clf().parse(a)
    assert d.confidence == 0.92 and d.probabilities == {"accept": 0.92}


@pytest.mark.parametrize("fit", [5, float("nan"), float("inf"), -0.5, "3", None, True])
def test_parse_bad_fit_invalid(fit):
    a = jev_answers()
    a["fit"]["score"] = fit
    with pytest.raises(JevError):
        clf().parse(a)


def test_parse_fit_non_dict_invalid():
    a = jev_answers()
    a["fit"] = [3]
    with pytest.raises(JevError):
        clf().parse(a)
