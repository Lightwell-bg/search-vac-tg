from src.filtering.rules import RuleResult
from src.filtering.scorer import decide, rules_bonus
from src.jev.schemas import JevDecision
from src.llm.schemas import ReviewResult


def rules(score=0, tech=None):
    return RuleResult("pass", "x", score, hits={"strong_positive": tech or []})


def jev(decision="accept", fit=80, category="ai_llm"):
    return JevDecision(decision, decision, 0.9, fit, fit / 100 * 3, category, reason="jev reason")


def llm(fit=80, notify=True, **kw):
    return ReviewResult(fit_score=fit, should_notify=notify, reason=kw.pop("reason", "llm reason"), **kw)


def test_bonus_capped_at_10():
    assert rules_bonus(rules(100)) == 10
    assert rules_bonus(rules(250)) == 10
    assert rules_bonus(rules(35)) == 3
    assert rules_bonus(None) == 0


def test_jev_accept_score_is_fit_plus_bonus():
    d = decide(rules(50), jev("accept", 80), None, 65)
    assert d.accept and d.score == 85
    assert d.category == "ai_llm"
    assert d.reason == "jev reason"


def test_jev_accept_score_capped_at_100():
    d = decide(rules(100), jev("accept", 98), None, 65)
    assert d.score == 100


def test_jev_accept_below_notify_score_rejected():
    d = decide(rules(0), jev("accept", 60), None, 65)
    assert not d.accept and d.score == 60
    assert "< 65" in d.reason


def test_jev_accept_bonus_can_lift_over_threshold():
    d = decide(rules(50), jev("accept", 60), None, 65)
    assert d.accept and d.score == 65


def test_llm_combines_70_30():
    d = decide(rules(0), jev("review", 50), llm(90), 65)
    assert d.score == round(0.7 * 90 + 0.3 * 50) == 78
    assert d.accept


def test_llm_only_when_jev_missing():
    d = decide(rules(0), None, llm(70), 65)
    assert d.score == 70 and d.accept


def test_llm_should_notify_false_rejects_regardless_of_score():
    d = decide(rules(100), jev("review", 100), llm(100, notify=False), 65)
    assert not d.accept and d.score == 100


def test_llm_low_score_with_notify_true_rejected_and_explained():
    d = decide(rules(0), jev("review", 20), llm(40, reason="meh"), 65)
    assert not d.accept
    assert "score" in d.reason and "< 65" in d.reason


def test_llm_bonus_capped_at_10():
    d = decide(rules(500), None, llm(70), 65)
    assert d.score == 80


def test_llm_skills_and_category_fallback():
    d = decide(rules(50, ["python"]), jev("review", 50, "parsing"), llm(80, relevant_skills=[], category=""), 65)
    assert d.relevant_skills == ["python"]
    assert d.category == "parsing"
    d2 = decide(rules(0), jev("review", 50), llm(80, relevant_skills=["Go"], category="x"), 65)
    assert d2.relevant_skills == ["Go"] and d2.category == "x"


def test_reject_and_review_without_llm_do_not_accept():
    assert not decide(rules(50), jev("reject", 10), None, 65).accept
    assert not decide(rules(50), jev("review", 90), None, 65).accept
    d = decide(None, None, None, 65)
    assert not d.accept and d.reason == "no decision"
