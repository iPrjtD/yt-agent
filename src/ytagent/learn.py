"""Daily learning loop: pull each Short's YouTube Analytics, store it, and shift the format mix toward what
performs. Writes data/learned_mix.json (read by pipeline.format_shares) and data/PERFORMANCE.md.

Only formats with enough mature Shorts (published 2+ days ago) move, and each stays within 0.5x-2x of its
share in settings.yaml, so one lucky or unlucky Short cannot take over the mix.
"""

from __future__ import annotations

import json
import logging
import math
import statistics as st
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from ytagent.config import get_settings

log = logging.getLogger(__name__)

METRICS = ("views,engagedViews,averageViewPercentage,averageViewDuration,likes,comments,shares,"
           "subscribersGained")
MIN_PER_FORMAT = 5      # mature Shorts a format needs before its share moves
MATURE_DAYS = 2         # Shorts get most of their views in the first 48 h
SHARE_RANGE = (0.5, 2.0)


def fetch_stats(days: int = 28) -> dict[str, dict]:
    """Per-video analytics for the last `days` days, keyed by YouTube id."""
    from ytagent.youtube import analytics_api

    end = date.today()
    resp = analytics_api().reports().query(
        ids="channel==MINE", startDate=(end - timedelta(days=days)).isoformat(), endDate=end.isoformat(),
        metrics=METRICS, dimensions="video", sort="-views", maxResults=200).execute()
    cols = [c["name"] for c in resp["columnHeaders"]]
    return {row[0]: dict(zip(cols[1:], row[1:], strict=True)) for row in resp.get("rows", [])}


def performance(stats: dict) -> float:
    """One number per Short: reach (log views, so a single hit does not dominate) times how many viewers stayed
    past the first seconds (engaged views / views)."""
    views = stats.get("views") or 0
    if not views:
        return 0.0
    stayed = min(1.0, (stats.get("engagedViews") or 0) / views)
    return math.log1p(views) * (0.5 + stayed)


def learned_shares(base: dict[str, float], by_format: dict[str, list[float]]) -> dict[str, float]:
    """Scale each format's base share by how its Shorts perform against the channel average (bounded)."""
    scores = {f: st.mean(v) for f, v in by_format.items() if f in base and len(v) >= MIN_PER_FORMAT}
    if len(scores) < 2:
        return dict(base)
    overall = st.mean(scores.values()) or 1.0
    lo, hi = SHARE_RANGE
    raw = {f: (s * max(lo, min(hi, (scores[f] / overall) ** 2)) if f in scores else s)
           for f, s in base.items() if s > 0}
    total = sum(raw.values()) or 1.0
    return {f: round(v / total, 3) for f, v in raw.items()}


def run_learning() -> dict:
    from ytagent.db import session_scope
    from ytagent.models import Video

    settings = get_settings()
    stats = fetch_stats()
    now = datetime.now(UTC)
    by_format: dict[str, list[float]] = {}
    rows = []
    with session_scope() as s:
        for v in s.scalars(select(Video).where(Video.youtube_id.is_not(None), Video.status == "published")):
            data = stats.get(v.youtube_id)
            if data is None:
                continue
            v.meta = {**(v.meta or {}), "stats": {**data, "fetched": now.isoformat(timespec="minutes")}}
            fmt = (v.script or {}).get("pillar") or "?"
            published = v.publish_at or v.created_at
            if published and published.replace(tzinfo=published.tzinfo or UTC) < now - timedelta(days=MATURE_DAYS):
                by_format.setdefault(fmt, []).append(performance(data))
            rows.append((fmt, v.id, v.title or "", data, (v.meta or {}).get("quality", {}).get("score")))
    base = {name: f.share for name, f in settings.formats.items()}
    shares = learned_shares(base, by_format)
    evidence = {}
    for fmt in sorted({r[0] for r in rows}):
        mine = [r[3] for r in rows if r[0] == fmt]
        views = [d.get("views") or 0 for d in mine]
        evidence[fmt] = {
            "shorts": len(mine), "median_views": st.median(views) if views else 0,
            "stayed": round(sum(d.get("engagedViews") or 0 for d in mine) / max(1, sum(views)), 2),
            "avg_pct_viewed": round(st.mean([d.get("averageViewPercentage") or 0 for d in mine]), 1) if mine else 0,
            "subs": sum(d.get("subscribersGained") or 0 for d in mine)}
    data_dir = settings.data_dir
    (data_dir / "learned_mix.json").write_text(json.dumps(
        {"updated": now.isoformat(timespec="minutes"), "shares": shares, "evidence": evidence}, indent=2),
        encoding="utf-8")
    _write_report(data_dir / "PERFORMANCE.md", rows, evidence, shares, base)
    return {"videos": len(rows), "shares": shares}


def _write_report(path, rows, evidence, shares, base) -> None:
    rows = sorted(rows, key=lambda r: -(r[3].get("views") or 0))

    def line(r):
        fmt, vid, title, d, q = r
        stayed = (d.get("engagedViews") or 0) / max(1, d.get("views") or 0)
        return (f"| {vid} | {title[:50]} | {fmt} | {d.get('views', 0)} | {stayed:.0%} | "
                f"{d.get('averageViewPercentage', 0):.0f}% | {d.get('subscribersGained', 0)} | {q or '-'} |")

    head = "| id | title | format | views | stayed | % viewed | subs | quality |\n|---|---|---|---|---|---|---|---|\n"
    fmt_rows = "\n".join(
        f"| {f} | {e['shorts']} | {e['median_views']} | {e['stayed']:.0%} | {e['avg_pct_viewed']}% | {e['subs']} | "
        f"{base.get(f, 0):.0%} -> {shares.get(f, 0):.0%} |" for f, e in evidence.items())
    path.write_text(
        f"# Buzzbee performance (last 28 days, updated {datetime.now():%d %b %H:%M})\n\n"
        "\"stayed\" = viewers who did not swipe away in the first seconds (engaged views / views).\n\n"
        "## By format\n\n| format | Shorts | median views | stayed | % viewed | subs | share |\n"
        "|---|---|---|---|---|---|---|\n" + fmt_rows + "\n\n"
        "## Top 10\n\n" + head + "\n".join(line(r) for r in rows[:10]) + "\n\n"
        "## Bottom 10\n\n" + head + "\n".join(line(r) for r in rows[-10:]) + "\n", encoding="utf-8")
