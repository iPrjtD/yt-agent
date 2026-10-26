"""Command line entry point: `uv run ytagent --help`."""

from __future__ import annotations

import logging
import sys

import typer
from rich.console import Console
from rich.table import Table

from ytagent.config import ROOT, get_settings

app = typer.Typer(no_args_is_help=True, add_completion=False)
trends_app = typer.Typer(no_args_is_help=True, help="Trend research agent.")
app.add_typer(trends_app, name="trends")
youtube_app = typer.Typer(no_args_is_help=True, help="YouTube account connection.")
app.add_typer(youtube_app, name="youtube")
brand_app = typer.Typer(no_args_is_help=True, help="Channel brand assets.")
app.add_typer(brand_app, name="brand")
short_app = typer.Typer(no_args_is_help=True, help="Produce Shorts.")
app.add_typer(short_app, name="short")
llm_app = typer.Typer(no_args_is_help=True, help="Language model setup.")
app.add_typer(llm_app, name="llm")
autopilot_app = typer.Typer(no_args_is_help=True, help="Scheduled, hands-off production.")
app.add_typer(autopilot_app, name="autopilot")


@autopilot_app.command("run")
def autopilot_run(job: str = typer.Argument(..., help="ideas | produce | cleanup")) -> None:
    """Run one autopilot job now (this is what the scheduled tasks call)."""
    from ytagent.autopilot import run

    console.print(run(job))


@autopilot_app.command("install")
def autopilot_install() -> None:
    """Register the Windows scheduled tasks (current user, no admin rights needed)."""
    from ytagent.schedule import install

    for line in install():
        console.print(f"[green]installed[/] {line}")


@autopilot_app.command("uninstall")
def autopilot_uninstall() -> None:
    """Remove the scheduled tasks."""
    from ytagent.schedule import uninstall

    for name in uninstall():
        console.print(f"removed {name}")


@autopilot_app.command("status")
def autopilot_status() -> None:
    """Is the autopilot healthy? Tasks, last results, services, free quotas and your Shorts."""
    import re

    from sqlalchemy import func, select

    from ytagent.autopilot import _docker_ok, _ollama_ok, write_ready_list
    from ytagent.db import session_scope
    from ytagent.models import Video
    from ytagent.providers import images
    from ytagent.providers.llm import _exhausted
    from ytagent.schedule import status

    settings = get_settings()
    table = Table("task", "state", "last run", "last result", "next run", title="Scheduled tasks")
    for r in status():
        colour = "green" if r["result"] in ("OK", "running now", "not run yet") else "red"
        table.add_row(r["task"], r["state"], r["last"], f"[{colour}]{r['result']}[/]", r["next"])
    console.print(table)

    # Latest outcome of each job, from the autopilot logs.
    outcomes: dict[str, str] = {}
    for log_file in sorted((settings.data_dir / "logs").glob("autopilot-*.log"))[-3:]:
        for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"(\S+ \S+),\d+ INFO ytagent\.autopilot: (\w+) -> (.*?) \(log:", line)
            if m:
                outcomes[m.group(2)] = f"{m.group(1)}  {m.group(3)[:110]}"
    jobs = Table("job", "latest outcome", title="Latest job results")
    for job in ("review", "ideas", "produce", "cleanup"):
        jobs.add_row(job, outcomes.get(job, "-"))
    console.print(jobs)

    def ok(flag: bool) -> str:
        return "[green]up[/]" if flag else "[red]DOWN[/]"

    try:
        with session_scope() as s:
            counts = dict(s.execute(select(Video.status, func.count()).group_by(Video.status)).all())
        db_up = True
    except Exception:
        counts, db_up = {}, False
    skip = _exhausted()
    writers = [settings.llm.writer, *settings.llm.fallbacks.get("writer", [])]
    tts = [f"gemini/{m}" for m in settings.voice.gemini_tts_models]
    console.print(f"Services: Docker {ok(_docker_ok())} | database {ok(db_up)} | local AI (Ollama) {ok(_ollama_ok())}")
    console.print(f"Free quotas today: script models {sum(m.startswith('gemini/') and m not in skip for m in writers)}"
                  f" available | voice models {sum(m not in skip for m in tts)}/{len(tts)} available | "
                  f"AI scenes {images._used_today()}/{settings.visuals.ai_daily_cap} used")
    console.print(f"Shorts: {counts.get('pending_review', 0)} waiting for review | "
                  f"{counts.get('scheduled', 0)} scheduled | {counts.get('published', 0)} published")
    console.print(f"Review list: {write_ready_list()}")
    console.print("If something is DOWN or FAILED: `ytagent autopilot start` re-registers and enables everything; "
                  "`ytagent autopilot run-now <job>` runs a job immediately; logs are in data/logs/.")


@autopilot_app.command("start")
def autopilot_start() -> None:
    """(Re)register all scheduled tasks and enable them."""
    from ytagent.schedule import install, set_enabled

    install()
    for name in set_enabled(True):
        console.print(f"[green]enabled[/] {name}")


@autopilot_app.command("stop")
def autopilot_stop() -> None:
    """Pause the autopilot (tasks stay registered; `start` resumes)."""
    from ytagent.schedule import set_enabled

    for name in set_enabled(False):
        console.print(f"paused {name}")


@autopilot_app.command("run-now")
def autopilot_run_now(job: str = typer.Argument(..., help="review | ideas | produce | cleanup")) -> None:
    """Start a job right away in the background, exactly like the schedule does."""
    from ytagent.schedule import run_now

    console.print(f"started {job}" if run_now(job) else f"[red]no task for {job!r}[/]")


@short_app.command("upload")
def short_upload(video_id: int) -> None:
    """Upload a finished Short to YouTube as private, into the "Pending review" playlist."""
    from pathlib import Path

    from ytagent.db import session_scope
    from ytagent.models import Video
    from ytagent.publish import upload_private
    from ytagent.script import ShortScript

    with session_scope() as s:
        video = s.get(Video, video_id)
        if video is None:
            raise typer.BadParameter(f"no video {video_id}")
        if video.youtube_id:
            console.print(f"already uploaded: https://youtu.be/{video.youtube_id}")
            return
        final = Path(video.meta["dir"]) / "final.mp4"
        script = ShortScript.model_validate({"hook_visual_query": "", **video.script})
        youtube_id = upload_private(final, script, video.description)
        video.youtube_id = youtube_id
    console.print(f"[green]uploaded (private, Pending review):[/] https://youtu.be/{youtube_id}")


@short_app.command("done")
def short_done(video_id: int) -> None:
    """Mark a Short as uploaded/published (removes it from READY.md)."""
    _set_video_status(video_id, "published")


@short_app.command("reject")
def short_reject(video_id: int) -> None:
    """Mark a Short as rejected (not to be uploaded)."""
    _set_video_status(video_id, "rejected")


def _set_video_status(video_id: int, status: str) -> None:
    from ytagent.autopilot import write_ready_list
    from ytagent.db import session_scope
    from ytagent.models import Video

    with session_scope() as s:
        video = s.get(Video, video_id)
        if video is None:
            raise typer.BadParameter(f"no video {video_id}")
        video.status = status
    write_ready_list()
    console.print(f"video {video_id} -> {status}")


@llm_app.command("gemini-models")
def llm_gemini_models() -> None:
    """List Gemini models your GEMINI_API_KEY can use."""
    from ytagent.providers.llm import gemini_models

    for name in gemini_models():
        console.print(name)


@llm_app.command("test")
def llm_test(role: str = "writer") -> None:
    """Send a tiny prompt to a role's model and show which model answered."""
    from ytagent.providers import llm

    console.print(llm.complete(role, [{"role": "user", "content": "Reply with exactly: Buzzbee ready"}],
                               temperature=0))


@short_app.command("make")
def short_make(
    topic_id: int = typer.Option(None, help="Topic id (default: best scored topic)."),
    upload: bool = typer.Option(None, "--upload/--no-upload", help="Override publishing.mode."),
    fmt: str = typer.Option(None, "--format", help="facts | funny | motivation | story | trending | money | world"),
    fresh: bool = typer.Option(False, "--fresh", help="Write a new script even if an earlier attempt saved one."),
) -> None:
    """Research, write, voice, render and package (or upload privately) one Short."""
    from ytagent.db import job_run
    from ytagent.pipeline import make_short

    with job_run("short.make") as detail:
        detail.update(make_short(topic_id, upload, fmt, fresh))
    console.print(detail)


@short_app.command("list")
def short_list(limit: int = 20) -> None:
    """Recent videos and their status."""
    from sqlalchemy import select

    from ytagent.db import session_scope
    from ytagent.models import Video

    with session_scope() as s:
        table = Table("id", "status", "title", "youtube", "folder")
        for v in s.scalars(select(Video).order_by(Video.id.desc()).limit(limit)):
            table.add_row(str(v.id), v.status, (v.title or "")[:50], v.youtube_id or "-",
                          (v.meta or {}).get("dir", "")[-40:])
    console.print(table)


@brand_app.command("apply")
def brand_apply(yes: bool = typer.Option(False, "--yes", help="Apply for real (default is a dry run).")) -> None:
    """Retire old videos (private), set banner, watermark, description, keywords and playlists."""
    from ytagent.channel import apply_brand

    report = apply_brand(dry_run=not yes)
    for line in report.done:
        console.print(("[dry run] " if not yes else "") + line)


@brand_app.command("render")
def brand_render() -> None:
    """Render mascot, profile picture, banner and watermark into brand/."""
    from ytagent.brand import render_all

    ch = get_settings().channel
    for path in render_all(ch.name, ch.tagline):
        console.print(f"wrote {path.relative_to(ROOT)}")
for _stream in (sys.stdout, sys.stderr):  # titles carry emoji; a piped Windows console is cp1252
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
console = Console()


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "LiteLLM", "litellm", "phonemizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@app.command()
def doctor() -> None:
    """Check all prerequisites."""
    from ytagent.doctor import run_checks

    table = Table("check", "status", "detail", "needed for")
    blocking = 0
    for c in run_checks(get_settings()):
        status = "[green]ok[/]" if c.ok else ("[red]MISSING[/]" if c.needed_for == "now" else "[yellow]later[/]")
        blocking += (not c.ok) and c.needed_for == "now"
        table.add_row(c.name, status, c.detail, c.needed_for)
    console.print(table)
    raise typer.Exit(1 if blocking else 0)


@app.command("db-upgrade")
def db_upgrade() -> None:
    """Apply database migrations."""
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    console.print("[green]database up to date[/]")


def _show_channel(expected_id: str) -> bool:
    from ytagent.youtube import my_channel

    ch = my_channel()
    snip, stats, status = ch["snippet"], ch.get("statistics", {}), ch.get("status", {})
    table = Table("field", "value", show_header=False)
    for field, value in [
        ("channel id", ch["id"]), ("title", snip.get("title")), ("handle", snip.get("customUrl")),
        ("subscribers", stats.get("subscriberCount")), ("videos", stats.get("videoCount")),
        ("views", stats.get("viewCount")), ("long uploads", status.get("longUploadsStatus")),
        ("made for kids", status.get("madeForKids")),
    ]:
        table.add_row(field, str(value))
    console.print(table)
    if expected_id and ch["id"] != expected_id:
        console.print(f"[red]Signed into channel {ch['id']}, but settings.yaml expects {expected_id}.[/] "
                      "Run `ytagent youtube login` again and pick the right channel.")
        return False
    return True


@youtube_app.command("login")
def youtube_login() -> None:
    """Sign in with Google in the browser and choose the channel to manage."""
    from ytagent.youtube import TOKEN_PATH, login

    login()
    if not _show_channel(get_settings().channel.youtube_channel_id):
        TOKEN_PATH.unlink(missing_ok=True)
        raise typer.Exit(1)
    console.print("[green]Connected.[/] Token saved to secrets/ (not committed).")


@youtube_app.command("whoami")
def youtube_whoami() -> None:
    """Show the connected channel."""
    if not _show_channel(get_settings().channel.youtube_channel_id):
        raise typer.Exit(1)


@trends_app.command("collect")
def trends_collect() -> None:
    """Fetch all trend sources and merge them into topics."""
    from ytagent.db import job_run, session_scope
    from ytagent.trends.collector import collect

    with job_run("trends.collect") as detail, session_scope() as s:
        detail.update(collect(s, get_settings()))
    console.print(detail)


@trends_app.command("score")
def trends_score(llm_limit: int = typer.Option(20, help="Max topics sent to the LLM this run.")) -> None:
    """Score live topics (heuristics + LLM assessment)."""
    from ytagent.db import job_run, session_scope
    from ytagent.trends.scoring import score_topics

    with job_run("trends.score") as detail, session_scope() as s:
        detail.update(score_topics(s, get_settings(), llm_limit=llm_limit))
    console.print(detail)


@trends_app.command("top")
def trends_top(limit: int = 20) -> None:
    """Show the best topics the LLM has assessed (unassessed ones only have a provisional score)."""
    from sqlalchemy import func, select

    from ytagent.db import session_scope
    from ytagent.models import Topic, TrendSignal

    with session_scope() as s:
        n_sources = (select(func.count(func.distinct(TrendSignal.source)))
                     .where(TrendSignal.topic_id == Topic.id).scalar_subquery())
        rows = s.execute(
            select(Topic, n_sources).where(Topic.status == "scored")
            .order_by(Topic.score.desc()).limit(limit)
        ).all()
        table = Table("score", "topic", "src", "format", "angle")
        for topic, src in rows:
            table.add_row(f"{topic.score:.1f}", topic.title[:60], str(src),
                          topic.llm.get("best_format", "?"), (topic.llm.get("angle") or "")[:70])
    console.print(table)


@trends_app.command("run")
def trends_run(llm_limit: int = 20) -> None:
    """collect + score in one go (what the scheduler will call)."""
    trends_collect()
    trends_score(llm_limit=llm_limit)


if __name__ == "__main__":
    app()
