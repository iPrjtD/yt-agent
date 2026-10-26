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
from datetime import datetime
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


def _pid_alive(pid: int) -> bool:
    """Is a process with this id still running? (Windows: OpenProcess + exit code; elsewhere: signal 0.)"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@contextmanager
def lock(name: str = "autopilot", wait_s: int = 0):
    """One job at a time (rendering and the local models need the whole CPU).

    The lock holds the owner's process id: if that process is gone (crashed, or killed by Task Scheduler's
    time limit) the lock is stale and is taken over at once. A live owner is waited for up to `wait_s`.
    """
    path = get_settings().data_dir / f"{name}.lock"
    deadline = time.time() + wait_s
    while path.exists():
        try:
            owner = int(path.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            owner = 0
        if owner != os.getpid() and _pid_alive(owner):
            if time.time() < deadline:
                time.sleep(15)
                continue
            raise RuntimeError(f"another autopilot job is running ({path.name}, pid {owner})")
        log.info("taking over stale lock %s (pid %s is gone)", path.name, owner)
        break
    path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


# --- services ------------------------------------------------------------------------------------------------

DOCKER_TIMEOUT_S = 25  # right after the PC wakes, the Docker CLI can hang indefinitely without a limit


def _docker(*args: str, timeout: int = DOCKER_TIMEOUT_S) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["docker", *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout,
                              creationflags=NO_WINDOW)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("docker %s did not answer: %s", " ".join(args[:2]), type(exc).__name__)
        return None


def _docker_ok() -> bool:
    out = _docker("info", "--format", "{{.ServerVersion}}")
    return bool(out and out.returncode == 0 and out.stdout.strip())


def _wait_for_network(max_wait_s: int = 120) -> None:
    """After waking from sleep Wi-Fi takes a while; jobs that start immediately would fail or stall."""
    for _ in range(max_wait_s // 5):
        try:
            httpx.get("https://www.googleapis.com/generate_204", timeout=5)
            return
        except httpx.HTTPError:
            time.sleep(5)
    log.warning("network still unavailable after %ss", max_wait_s)


def _ollama_ok() -> bool:
    try:
        return httpx.get(f"{get_settings().env.ollama_api_base}/api/version", timeout=5).is_success
    except httpx.HTTPError:
        return False


def ensure_services(need_models: bool = True) -> None:
    """Start Docker Desktop / the containers / Ollama if they are not up yet (e.g. right after boot or wake)."""
    _wait_for_network()
    if not _docker_ok() and DOCKER_DESKTOP.exists():
        log.info("starting Docker Desktop")
        subprocess.Popen([str(DOCKER_DESKTOP)], creationflags=NO_WINDOW)
        for _ in range(36):
            time.sleep(5)
            if _docker_ok():
                break
    if not _docker_ok():
        raise RuntimeError("Docker is not responding")
    _docker("compose", "up", "-d", "db", "searxng", timeout=120)
    if need_models and not _ollama_ok():
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
            log.info("services ready (docker, database%s)", ", ollama" if need_models else "")
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
                + (f"https://youtu.be/{v.youtube_id} (private)" if v.youtube_id
                   else f"`{Path((v.meta or {}).get('dir', '')) / 'final.mp4'}`") + " |" for v in videos]
        scheduled = s.scalars(select(Video).where(Video.status == "scheduled").order_by(Video.publish_at)).all()
        upcoming = [f"| {v.id} | {v.title} | {v.publish_at:%a %d %b %H:%M} UTC | https://youtu.be/{v.youtube_id} |"
                    for v in scheduled if v.publish_at]
    path.write_text(
        "# Shorts ready for review\n\n"
        "In YouTube Studio → Content → Playlists → **Pending review**: watch each Short, then add it to the\n"
        "**Approved** playlist (it gets published at the next slot) or **Rejected**. Shorts without a YouTube\n"
        "link were not uploaded: upload `final.mp4` yourself with the text in `publish.txt`.\n\n"
        "| id | title | format | video |\n|---|---|---|---|\n" + "\n".join(rows) + "\n\n"
        "## Scheduled\n\n| id | title | goes live | link |\n|---|---|---|---|\n" + "\n".join(upcoming) + "\n",
        encoding="utf-8")
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
    """Delete stock/archive cache older than 7 days, and the local video files of finished Shorts (published,
    rejected, failed) after `keep_video_days`: they live on YouTube; scripts and plans are kept."""
    from ytagent.db import session_scope
    from ytagent.models import Video

    settings = get_settings()
    data = settings.data_dir
    cutoff = time.time() - 7 * 86400
    removed = 0
    for path in (data / "cache" / "pixabay").glob("*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    for path in (data / "logs").glob("autopilot-*.log"):
        if path.stat().st_mtime < time.time() - 30 * 86400:
            path.unlink(missing_ok=True)
    video_cutoff = time.time() - settings.autopilot.keep_video_days * 86400
    freed = 0
    with session_scope() as s:
        done = s.scalars(select(Video).where(Video.status.in_(["published", "rejected", "failed"]))).all()
        folders = [Path(v.meta["dir"]) for v in done if (v.meta or {}).get("dir")]
    for folder in folders:
        for name in ("final.mp4", "voice.wav"):
            path = folder / name
            if path.is_file() and path.stat().st_mtime < video_cutoff:
                path.unlink(missing_ok=True)
                freed += 1
    return {"cache_files_removed": removed, "video_files_removed": freed}


def job_review() -> dict:
    """Publish/schedule Shorts you moved to "Approved"; handle "Rejected"; notice Shorts that went live."""
    from ytagent.review import sync

    result = sync()
    for title in result.get("scheduled", []) + result.get("published_now", []):
        notify("Buzzbee: Short scheduled 🐝", title)
    return result


JOBS = {"ideas": job_ideas, "produce": job_produce, "cleanup": job_cleanup, "review": job_review}
LIGHT_JOBS = {"review"}  # quick API jobs: own lock, no local models needed


def run(job: str) -> dict:
    log_path = setup_logging()
    log.info("=== %s ===", job)
    try:
        # Heavy jobs queue behind each other (e.g. ideas while a Short renders) instead of being skipped.
        with lock("review" if job in LIGHT_JOBS else "autopilot", wait_s=0 if job in LIGHT_JOBS else 1200):
            ensure_services(need_models=job not in LIGHT_JOBS | {"cleanup"})
            result = JOBS[job]()
            write_ready_list()
    except Exception as exc:
        log.exception("%s failed", job)
        result = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    log.info("%s -> %s (log: %s)", job, result, log_path.name)
    return result
