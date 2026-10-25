"""Autopilot: short jobs run by Windows Task Scheduler (ideas every few hours, Shorts overnight, cleanup).

Each job checks the services it needs, takes a lock so runs never overlap, logs to data/logs/ and updates
data/READY.md with the Shorts waiting for review/upload.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import select

from ytagent.config import ROOT, get_settings
from ytagent.proc import NO_WINDOW

log = logging.getLogger("ytagent.autopilot")
DOCKER_DESKTOP = Path(r"C:\Program Files\Docker\Docker\Docker Desktop.exe")


def setup_logging() -> Path:
    logs = get_settings().data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / f"autopilot-{datetime.now():%Y-%m-%d}.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for noisy in ("httpx", "LiteLLM", "litellm", "phonemizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return path


@contextmanager
def lock(name: str = "autopilot", stale_after: timedelta = timedelta(hours=3)):
    """One job at a time (rendering and the local models need the whole CPU). Stale locks are ignored."""
    path = get_settings().data_dir / f"{name}.lock"
    if path.exists() and datetime.now().timestamp() - path.stat().st_mtime < stale_after.total_seconds():
        raise RuntimeError(f"another autopilot job is running ({path.name})")
    path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


# --- services ------------------------------------------------------------------------------------------------


def _docker_ok() -> bool:
    out = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True,
                         creationflags=NO_WINDOW)
    return out.returncode == 0 and bool(out.stdout.strip())


def _ollama_ok() -> bool:
    try:
        return httpx.get(f"{get_settings().env.ollama_api_base}/api/version", timeout=5).is_success
    except httpx.HTTPError:
        return False


def ensure_services() -> None:
    """Start Docker Desktop / the containers / Ollama if they are not up yet (e.g. right after boot)."""
    if not _docker_ok() and DOCKER_DESKTOP.exists():
        log.info("starting Docker Desktop")
        subprocess.Popen([str(DOCKER_DESKTOP)], creationflags=NO_WINDOW)
        for _ in range(36):
            time.sleep(5)
            if _docker_ok():
                break
    if not _docker_ok():
        raise RuntimeError("Docker is not running")
    subprocess.run(["docker", "compose", "up", "-d", "db", "searxng"], cwd=ROOT, capture_output=True,
                   creationflags=NO_WINDOW)
    if not _ollama_ok():
        exe = shutil.which("ollama") or r"D:\tools\Ollama\ollama.exe"
        log.info("starting Ollama server")
        subprocess.Popen([exe, "serve"], creationflags=NO_WINDOW, env={**os.environ,
                         "OLLAMA_MODELS": os.environ.get("OLLAMA_MODELS", r"D:\tools\ollama-models")})
        for _ in range(24):
            time.sleep(5)
            if _ollama_ok():
                break
    for _ in range(12):  # the database container may still be starting
        try:
            from sqlalchemy import text

            from ytagent.db import get_engine
            with get_engine().connect() as conn:
                conn.execute(text("select 1"))
            return
        except Exception:
            time.sleep(5)
    raise RuntimeError("database not reachable")


# --- notifications and the ready list ------------------------------------------------------------------------


def notify(title: str, message: str) -> None:
    """Phone push via ntfy.sh when NTFY_TOPIC is set (free, no account); always logged."""
    log.info("%s: %s", title, message)
    topic = get_settings().env.ntfy_topic
    if not topic:
        return
    try:
        httpx.post(f"https://ntfy.sh/{topic}", content=message.encode("utf-8"),
                   headers={"Title": title.encode("utf-8"), "Tags": "bee"}, timeout=15)
    except httpx.HTTPError as exc:
        log.warning("ntfy failed: %s", exc)


def write_ready_list() -> Path:
    """data/READY.md: Shorts waiting for you to review and upload (manual mode)."""
    from ytagent.db import session_scope
    from ytagent.models import Video

    path = get_settings().data_dir / "READY.md"
    with session_scope() as s:
        videos = s.scalars(select(Video).where(Video.status == "pending_review").order_by(Video.id)).all()
        rows = [f"| {v.id} | {v.title} | {(v.script or {}).get('pillar', '')} | "
                f"`{Path((v.meta or {}).get('dir', '')) / 'final.mp4'}` |" for v in videos]
    path.write_text(
        "# Shorts ready for review\n\n"
        "Upload in YouTube Studio with the title/description/tags from `publish.txt` in each folder, then run\n"
        "`uv run ytagent short done <id>` (or `short reject <id>`).\n\n"
        "| id | title | format | video |\n|---|---|---|---|\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


# --- jobs ----------------------------------------------------------------------------------------------------


def job_ideas() -> dict:
    """Collect trend + idea sources and score new topics (local model)."""
    from ytagent.db import session_scope
    from ytagent.trends.collector import collect
    from ytagent.trends.scoring import score_topics

    settings = get_settings()
    with session_scope() as s:
        collected = collect(s, settings)
    with session_scope() as s:
        scored = score_topics(s, settings, llm_limit=settings.autopilot.score_limit)
    return {"collected": collected.get("items"), "new": collected.get("created"), **scored}


def gemini_writer_available() -> bool:
    from ytagent.providers.llm import _exhausted

    cfg = get_settings().llm
    chain = [cfg.writer, *cfg.fallbacks.get("writer", [])]
    return any(m.startswith("gemini/") and m not in _exhausted() for m in chain)


def job_produce() -> dict:
    """Make one Short (format mix decides the topic); retries once with another topic on failure."""
    from ytagent.db import session_scope
    from ytagent.media.voice import VoiceUnavailable
    from ytagent.models import Topic, Video
    from ytagent.pipeline import make_short

    settings = get_settings()
    if settings.autopilot.require_gemini_writer and not gemini_writer_available():
        notify("Buzzbee: production postponed", "Gemini daily quota is used up; the next run will try again.")
        return {"skipped": "gemini quota"}
    last_error = ""
    for _ in range(2):
        try:
            result = make_short()
            if result["status"] == "pending_review":
                notify("Buzzbee: new Short ready 🐝", f"#{result['video_id']} {result['title']}")
            return result
        except VoiceUnavailable:  # Buzzbee's voice quota is used up: keep the topic, try at the next run
            notify("Buzzbee: production postponed", "Gemini voice quota is used up; the next run will try again.")
            return {"skipped": "voice quota"}
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"[:300]
            log.exception("produce failed")
            with session_scope() as s:  # don't pick the same topic again
                video = s.scalars(select(Video).order_by(Video.id.desc()).limit(1)).first()
                if video and video.topic_id and video.status == "failed":
                    s.get(Topic, video.topic_id).status = "rejected"
    notify("Buzzbee: production failed", last_error)
    return {"error": last_error}


def job_cleanup() -> dict:
    """Delete stock/archive cache older than 14 days and render leftovers; keep finished videos."""
    data = get_settings().data_dir
    cutoff = time.time() - 14 * 86400
    removed = 0
    for path in (data / "cache" / "pixabay").glob("*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    for path in (data / "logs").glob("autopilot-*.log"):
        if path.stat().st_mtime < time.time() - 30 * 86400:
            path.unlink(missing_ok=True)
    return {"cache_files_removed": removed}


JOBS = {"ideas": job_ideas, "produce": job_produce, "cleanup": job_cleanup}


def run(job: str) -> dict:
    log_path = setup_logging()
    log.info("=== %s ===", job)
    try:
        with lock():
            if job != "cleanup":
                ensure_services()
            result = JOBS[job]()
            write_ready_list()
    except Exception as exc:
        log.exception("%s failed", job)
        result = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    log.info("%s -> %s (log: %s)", job, result, log_path.name)
    return result
