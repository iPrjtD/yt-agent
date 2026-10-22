from datetime import timedelta

from ytagent.config import load_settings
from ytagent.models import TrendSignal, utcnow
from ytagent.trends.scoring import TopicAssessment, combine, heuristic

SETTINGS = load_settings()


def _sig(source: str, strength: float, related: int = 0, hours_ago: float = 0) -> TrendSignal:
    return TrendSignal(source=source, external_id=source, title="t", strength=strength, related=related,
                       observed_at=utcnow() - timedelta(hours=hours_ago))


def _assessment(**kw) -> TopicAssessment:
    base = dict(summary="s", category="tech", niche_fit=8, ad_safety=9, sensitive=False, blocked=False,
                evergreen=False, content_format="facts", best_format="short", angle="a")
    return TopicAssessment(**{**base, **kw})


def test_heuristic_uses_latest_signal_per_source():
    h = heuristic([_sig("wikipedia", 0.2, hours_ago=5), _sig("wikipedia", 0.9), _sig("google_news", 0.4, 6)],
                  first_seen_hours=0, half_life_h=18)
    assert h["traffic"] == 0.9
    assert h["sources"] == 2
    assert h["freshness"] == 1.0


def test_more_sources_score_higher():
    one = heuristic([_sig("a", 0.5)], 2, 18)
    three = heuristic([_sig("a", 0.5), _sig("b", 0.5), _sig("c", 0.5)], 2, 18)
    assert combine(three, _assessment(), SETTINGS) > combine(one, _assessment(), SETTINGS)


def test_blocked_or_sensitive_scores_zero():
    h = heuristic([_sig("a", 1.0)], 0, 18)
    assert combine(h, _assessment(sensitive=True), SETTINGS) == 0
    assert combine(h, _assessment(blocked=True), SETTINGS) == 0


def test_score_range():
    h = heuristic([_sig("a", 1.0, related=10), _sig("b", 1.0), _sig("c", 1.0), _sig("d", 1.0)], 0, 18)
    assert combine(h, _assessment(niche_fit=10, ad_safety=10), SETTINGS) == 100.0
