"""Command line entry point: `uv run ytagent --help`."""

from __future__ import annotations

import logging

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
console = Console()


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "LiteLLM", "litellm"):
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
