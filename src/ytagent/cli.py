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
