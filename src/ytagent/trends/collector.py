"""Fetch all sources, merge items into topics (exact key, then embedding similarity), store signals."""

from __future__ import annotations

import logging
from datetime import timedelta

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ytagent.config import Settings
from ytagent.models import Topic, TrendSignal, utcnow
from ytagent.providers import llm
from ytagent.trends.base import USER_AGENT, TrendItem, topic_key
from ytagent.trends.sources import build_sources

log = logging.getLogger(__name__)

# Cosine distance under which two items count as the same story (bge-m3; tune with real data).
MERGE_DISTANCE = 0.18


def fetch_all(settings: Settings) -> tuple[list[TrendItem], dict[str, str]]:
    items: list[TrendItem] = []
    errors: dict[str, str] = {}
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True) as client:
        for source in build_sources(settings):
            try:
                got = source.fetch(client)
                log.info("%s: %d items", source.name, len(got))
                items += got
            except Exception as exc:  # one broken source must not stop the run
                log.warning("%s failed: %s", source.name, exc)
                errors[source.name] = str(exc)[:300]
    seen: set[tuple[str, str]] = set()
    unique = [i for i in items if (i.source, i.external_id) not in seen and not seen.add((i.source, i.external_id))]
    return unique, errors


def _embed_text(item: TrendItem) -> str:
    return f"{item.title}. {item.context[:300]}".strip()


def _match_topic(s: Session, item: TrendItem, vector: list[float], since) -> Topic | None:
    topic = s.scalar(select(Topic).where(Topic.key == topic_key(item.title)))
    if topic:
        return topic
    distance = Topic.embedding.cosine_distance(vector)
    row = s.execute(
        select(Topic, distance.label("d"))
        .where(Topic.embedding.is_not(None), Topic.last_seen_at >= since, Topic.status != "expired")
        .order_by(distance)
        .limit(1)
    ).first()
    return row[0] if row and row.d <= MERGE_DISTANCE else None


def store(s: Session, items: list[TrendItem], vectors: list[list[float]], keep_days: int) -> dict[str, int]:
    now = utcnow()
    since = now - timedelta(days=keep_days)
    created = merged = 0
    for item, vector in zip(items, vectors, strict=True):
        topic = _match_topic(s, item, vector, since)
        if topic is None:
            topic = Topic(key=topic_key(item.title), title=item.title, embedding=vector, urls=[])
            s.add(topic)
            created += 1
        else:
            merged += 1
            if topic.status == "expired":
                topic.status = "new"
        topic.last_seen_at = now
        if item.url and item.url not in topic.urls:
            topic.urls = [*topic.urls, item.url][-10:]
        s.add(TrendSignal(
            topic=topic, source=item.source, external_id=item.external_id[:500], title=item.title[:500],
            url=item.url, strength=item.strength, related=item.related, observed_at=now,
            raw={**item.raw, "context": item.context[:1000],
                 "published_at": item.published_at.isoformat() if item.published_at else None},
        ))
        s.flush()  # make the new topic visible to the next item's similarity search
    expired = s.execute(
        update(Topic).where(Topic.last_seen_at < since, Topic.status.in_(["new", "scored"]))
        .values(status="expired")
    ).rowcount
    return {"created": created, "merged": merged, "expired": expired}


def collect(s: Session, settings: Settings) -> dict:
    items, errors = fetch_all(settings)
    vectors = llm.embed([_embed_text(i) for i in items]) if items else []
    stats = store(s, items, vectors, settings.trends.keep_days)
    return {"items": len(items), "source_errors": errors, **stats}
