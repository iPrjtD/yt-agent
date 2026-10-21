"""Database schema. Changes go through Alembic migrations (see migrations/)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Must match the embedding model in config/settings.yaml (bge-m3 = 1024).
# Switching to a model with another size needs a migration plus a re-embed.
EMBED_DIMS = 1024


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


class Topic(Base):
    """A candidate subject for a video, merged from one or more trend signals."""

    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(200), unique=True)
    title: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str | None] = mapped_column(Text)
    # new -> scored -> selected | rejected | expired; selected -> used once a video exists
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    score: Mapped[float | None] = mapped_column(Float, index=True)
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(default=dict)
    llm: Mapped[dict[str, Any]] = mapped_column(default=dict)
    urls: Mapped[list[Any]] = mapped_column(default=list)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIMS))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    signals: Mapped[list[TrendSignal]] = relationship(back_populates="topic", cascade="all, delete-orphan")


class TrendSignal(Base):
    """One observation of a topic on one source at one time (several per topic give velocity)."""

    __tablename__ = "trend_signals"
    __table_args__ = (Index("ix_trend_signals_source_ext", "source", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(40))
    external_id: Mapped[str] = mapped_column(String(500))
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str | None] = mapped_column(Text)
    # Source-normalised popularity: 0 (weak) .. 1 (top of that source right now).
    strength: Mapped[float] = mapped_column(Float, default=0.0)
    related: Mapped[int] = mapped_column(Integer, default=0)
    raw: Mapped[dict[str, Any]] = mapped_column(default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    topic: Mapped[Topic] = relationship(back_populates="signals")


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id", ondelete="SET NULL"), index=True)
    format: Mapped[str] = mapped_column(String(10))  # short | long
    # draft -> rendering -> pending_review -> approved | rejected -> scheduled -> published | failed
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    title: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[Any]] = mapped_column(default=list)
    script: Mapped[dict[str, Any]] = mapped_column(default=dict)
    edit_plan: Mapped[dict[str, Any]] = mapped_column(default=dict)
    youtube_id: Mapped[str | None] = mapped_column(String(20), unique=True)
    publish_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class VideoMetric(Base):
    __tablename__ = "video_metrics"

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    window: Mapped[str] = mapped_column(String(10))  # 1h | 24h | 7d | 28d
    views: Mapped[int | None] = mapped_column(Integer)
    likes: Mapped[int | None] = mapped_column(Integer)
    comments: Mapped[int | None] = mapped_column(Integer)
    subs_gained: Mapped[int | None] = mapped_column(Integer)
    avg_view_duration_s: Mapped[float | None] = mapped_column(Float)
    avg_view_pct: Mapped[float | None] = mapped_column(Float)
    ctr: Mapped[float | None] = mapped_column(Float)
    raw: Mapped[dict[str, Any]] = mapped_column(default=dict)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PolicyDoc(Base):
    """A YouTube policy / help page tracked by the platform knowledge agent."""

    __tablename__ = "policy_docs"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str | None] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str | None] = mapped_column(String(64))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIMS))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlaybookEntry(Base):
    """A learned rule ("question hooks beat statement hooks for tech Shorts"), with its evidence."""

    __tablename__ = "playbook_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))  # hook | title | topic | format | timing | thumbnail
    insight: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict[str, Any]] = mapped_column(default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str] = mapped_column(String(60), index=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | ok | failed
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
