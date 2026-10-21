"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-10-22 02:42:29.316788
"""
from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table('job_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('job', sa.String(length=60), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_job_runs_job'), 'job_runs', ['job'], unique=False)
    op.create_table('playbook_entries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('insight', sa.Text(), nullable=False),
    sa.Column('evidence', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('policy_docs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=True),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=True),
    sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=1024), nullable=True),
    sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('changed_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('url')
    )
    op.create_table('topics',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=200), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('score', sa.Float(), nullable=True),
    sa.Column('score_breakdown', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('llm', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('urls', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=1024), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key')
    )
    op.create_index(op.f('ix_topics_last_seen_at'), 'topics', ['last_seen_at'], unique=False)
    op.create_index(op.f('ix_topics_score'), 'topics', ['score'], unique=False)
    op.create_index(op.f('ix_topics_status'), 'topics', ['status'], unique=False)
    op.create_table('trend_signals',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('topic_id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=40), nullable=False),
    sa.Column('external_id', sa.String(length=500), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('url', sa.Text(), nullable=True),
    sa.Column('strength', sa.Float(), nullable=False),
    sa.Column('related', sa.Integer(), nullable=False),
    sa.Column('raw', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['topic_id'], ['topics.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_trend_signals_observed_at'), 'trend_signals', ['observed_at'], unique=False)
    op.create_index('ix_trend_signals_source_ext', 'trend_signals', ['source', 'external_id'], unique=False)
    op.create_index(op.f('ix_trend_signals_topic_id'), 'trend_signals', ['topic_id'], unique=False)
    op.create_table('videos',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('topic_id', sa.Integer(), nullable=True),
    sa.Column('format', sa.String(length=10), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('tags', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('script', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('edit_plan', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('youtube_id', sa.String(length=20), nullable=True),
    sa.Column('publish_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('meta', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['topic_id'], ['topics.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('youtube_id')
    )
    op.create_index(op.f('ix_videos_status'), 'videos', ['status'], unique=False)
    op.create_index(op.f('ix_videos_topic_id'), 'videos', ['topic_id'], unique=False)
    op.create_table('video_metrics',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('video_id', sa.Integer(), nullable=False),
    sa.Column('window', sa.String(length=10), nullable=False),
    sa.Column('views', sa.Integer(), nullable=True),
    sa.Column('likes', sa.Integer(), nullable=True),
    sa.Column('comments', sa.Integer(), nullable=True),
    sa.Column('subs_gained', sa.Integer(), nullable=True),
    sa.Column('avg_view_duration_s', sa.Float(), nullable=True),
    sa.Column('avg_view_pct', sa.Float(), nullable=True),
    sa.Column('ctr', sa.Float(), nullable=True),
    sa.Column('raw', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['video_id'], ['videos.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_video_metrics_video_id'), 'video_metrics', ['video_id'], unique=False)
    # Similarity search used to merge the same story reported by different sources.
    op.execute("CREATE INDEX ix_topics_embedding_hnsw ON topics USING hnsw (embedding vector_cosine_ops)")


def downgrade() -> None:
    op.drop_index(op.f('ix_video_metrics_video_id'), table_name='video_metrics')
    op.drop_table('video_metrics')
    op.drop_index(op.f('ix_videos_topic_id'), table_name='videos')
    op.drop_index(op.f('ix_videos_status'), table_name='videos')
    op.drop_table('videos')
    op.drop_index(op.f('ix_trend_signals_topic_id'), table_name='trend_signals')
    op.drop_index('ix_trend_signals_source_ext', table_name='trend_signals')
    op.drop_index(op.f('ix_trend_signals_observed_at'), table_name='trend_signals')
    op.drop_table('trend_signals')
    op.drop_index(op.f('ix_topics_status'), table_name='topics')
    op.drop_index(op.f('ix_topics_score'), table_name='topics')
    op.drop_index(op.f('ix_topics_last_seen_at'), table_name='topics')
    op.drop_table('topics')
    op.drop_table('policy_docs')
    op.drop_table('playbook_entries')
    op.drop_index(op.f('ix_job_runs_job'), table_name='job_runs')
    op.drop_table('job_runs')
