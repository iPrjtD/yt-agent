"""Engine, sessions and a job-run helper."""

from __future__ import annotations

import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ytagent.config import get_settings
from ytagent.models import JobRun, utcnow


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    # connect_timeout: a wedged Docker port after the PC wakes must not hang a job forever.
    return create_engine(get_settings().env.database_url, pool_pre_ping=True, connect_args={"connect_timeout": 10})


@contextmanager
def session_scope() -> Iterator[Session]:
    session = sessionmaker(bind=get_engine(), expire_on_commit=False)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def job_run(name: str) -> Iterator[dict[str, Any]]:
    """Record a job in job_runs; the caller fills the yielded dict with result details."""
    with session_scope() as s:
        run = JobRun(job=name)
        s.add(run)
        s.flush()
        run_id = run.id
    detail: dict[str, Any] = {}
    try:
        yield detail
    except Exception as exc:
        with session_scope() as s:
            row = s.get(JobRun, run_id)
            row.status, row.detail, row.finished_at = "failed", detail, utcnow()
            row.error = f"{exc}\n{traceback.format_exc()}"
        raise
    with session_scope() as s:
        row = s.get(JobRun, run_id)
        row.status, row.detail, row.finished_at = "ok", detail, utcnow()
