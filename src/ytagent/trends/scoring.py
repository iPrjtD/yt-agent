"""Topic score (0-100) = weighted heuristic signals + LLM judgement of fit and ad-safety."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ytagent.config import ROOT, Settings
from ytagent.models import Topic, TrendSignal, utcnow
from ytagent.providers import llm

log = logging.getLogger(__name__)

SIGNAL_WINDOW = timedelta(hours=48)
IDEA_SOURCES = {"wikipedia_dyk", "evergreen", "motivation"}


class TopicAssessment(BaseModel):
    summary: str
    category: str = Field(description="one or two words, e.g. tech, sports, entertainment, finance")
    niche_fit: int = Field(ge=0, le=10)
    ad_safety: int = Field(ge=0, le=10)
    sensitive: bool
    blocked: bool
    evergreen: bool
    content_format: Literal["facts", "funny", "motivation", "story", "trending"] | None = None
    best_format: Literal["short", "long", "both"]
    angle: str


def heuristic(signals: list[TrendSignal], first_seen_hours: float, half_life_h: float) -> dict[str, float]:
    latest: dict[str, TrendSignal] = {}
    for sig in sorted(signals, key=lambda x: x.observed_at):
        latest[sig.source] = sig
    traffic = max((s.strength for s in latest.values()), default=0.0)
    sources = len(latest)
    max_related = max((s.related for s in latest.values()), default=0)
    corroboration = 0.7 * min(1.0, (sources - 1) / 3) + 0.3 * min(1.0, max_related / 8)
    freshness = 0.5 ** (first_seen_hours / half_life_h)
    return {"traffic": round(traffic, 4), "corroboration": round(corroboration, 4),
            "freshness": round(freshness, 4), "sources": sources}


def combine(h: dict[str, float], assessment: TopicAssessment | None, settings: Settings) -> float:
    w = settings.trends.scoring
    if assessment is not None and (assessment.blocked or assessment.sensitive):
        return 0.0
    # Until the LLM has looked at a topic, assume an average fit so it can still rank for assessment.
    fit = 0.5 if assessment is None else (assessment.niche_fit / 10) * (assessment.ad_safety / 10)
    total = (w.w_traffic * h["traffic"] + w.w_corroboration * h["corroboration"]
             + w.w_freshness * h["freshness"] + w.w_llm_fit * fit)
    weights = w.w_traffic + w.w_corroboration + w.w_freshness + w.w_llm_fit
    return round(100 * total / weights, 2)


def assess(topic: Topic, signals: list[TrendSignal], settings: Settings) -> TopicAssessment:
    ch = settings.channel
    channel_desc = "; ".join(ch.niches) if ch.niches else "broad entertainment and information"
    context = " | ".join(dict.fromkeys(s.raw.get("context", "") for s in signals if s.raw.get("context")))
    prompt = (ROOT / "prompts" / "topic_assess.md").read_text(encoding="utf-8").format(
        channel_desc=channel_desc, geo=ch.geo, language=ch.language,
        blocked="; ".join(ch.blocked_topics) or "nothing listed",
        title=topic.title,
        signals=", ".join(f"{s.source} strength {s.strength:.2f}" for s in signals[-6:]),
        context=context[:1500] or "none",
    )
    return llm.complete_json("fast", [{"role": "user", "content": prompt}], TopicAssessment)


def score_topics(s: Session, settings: Settings, llm_limit: int = 20) -> dict[str, int]:
    """Recompute heuristics for all live topics; ask the LLM about the best unassessed ones."""
    now = utcnow()
    topics = s.scalars(
        select(Topic).where(Topic.status.in_(["new", "scored"])).options(selectinload(Topic.signals))
    ).all()
    ranked = []
    for topic in topics:
        recent = [x for x in topic.signals if x.observed_at >= now - SIGNAL_WINDOW] or topic.signals
        hours = (now - topic.first_seen_at).total_seconds() / 3600
        h = heuristic(recent, hours, settings.trends.scoring.freshness_half_life_h)
        assessment = TopicAssessment(**topic.llm) if topic.llm else None
        ranked.append((combine(h, assessment, settings), topic, recent, h))

    assessed = rejected = 0
    # Evergreen ideas first (the channel's core content), then everything else by provisional score.
    ranked.sort(key=lambda r: (any(sig.source in IDEA_SOURCES for sig in r[2]), r[0]), reverse=True)
    for provisional, topic, recent, h in ranked:
        assessment = TopicAssessment(**topic.llm) if topic.llm else None
        if assessment is None and assessed < llm_limit:
            try:
                assessment = assess(topic, recent, settings)
                topic.llm = assessment.model_dump()
                topic.summary = assessment.summary
                assessed += 1
            except Exception as exc:
                log.warning("assessment failed for %r: %s", topic.title, exc)
        topic.score = combine(h, assessment, settings) if assessment else provisional
        topic.score_breakdown = {**h, "llm_fit": None if assessment is None else
                                 round(assessment.niche_fit * assessment.ad_safety / 100, 3)}
        if assessment is None:
            continue
        if assessment.blocked or assessment.sensitive:
            topic.status = "rejected"
            rejected += 1
        else:
            topic.status = "scored"
    return {"topics": len(ranked), "assessed": assessed, "rejected": rejected}
