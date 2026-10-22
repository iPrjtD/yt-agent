"""End-to-end Short production: topic -> brief -> script -> voice -> visuals -> render -> QA -> package/upload."""

from __future__ import annotations

import json
import logging
import math
import re
import shutil
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import select

from ytagent.config import get_settings
from ytagent.db import session_scope
from ytagent.media.archive import find_archive_clip
from ytagent.media.render import Segment, pick_music, qa, render_short
from ytagent.media.visuals import Visual, find_visual, text_card
from ytagent.media.voice import narrate
from ytagent.models import Topic, Video
from ytagent.publish import full_description, upload_private, write_package
from ytagent.research import Brief, Source, build_format_brief
from ytagent.script import ShortScript, write_short

log = logging.getLogger(__name__)

OUTRO_CARD = "Follow for more buzz"
SHOT_SECONDS = 4.0


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "short"


def _previous_attempt(work: Path) -> Path | None:
    """An earlier folder for the same topic that already has a brief and script (resume after a failure)."""
    slug = work.name.split("-", 1)[1]
    for folder in sorted(work.parent.glob(f"*-{slug}"), reverse=True):
        if folder != work and (folder / "script.json").exists() and (folder / "brief.json").exists():
            return folder
    return None


def _format_of(topic: Topic) -> str:
    fmt = topic.llm.get("content_format") or ("motivation" if " on " in topic.title else "facts")
    return fmt if fmt in get_settings().formats else "facts"


def pick_topic(topic_id: int | None, fmt: str | None = None) -> tuple[Topic, str]:
    """Explicit topic, else the best topic of the format furthest below its target share of recent videos."""
    formats = get_settings().formats
    with session_scope() as s:
        if topic_id:
            topic = s.get(Topic, topic_id)
            if topic is None:
                raise ValueError(f"topic {topic_id} not found")
            return topic, fmt or _format_of(topic)
        recent = s.scalars(select(Video).where(Video.status != "failed").order_by(Video.id.desc()).limit(20)).all()
        counts = Counter(v.script.get("pillar") for v in recent if v.script)
        total = max(1, sum(counts.values()))
        order = [fmt] if fmt else sorted(formats, key=lambda f: counts[f] / total - formats[f].share)
        for want in order:
            topic = s.scalar(
                select(Topic).where(Topic.status == "scored", Topic.llm["content_format"].astext == want,
                                    Topic.llm["best_format"].astext.in_(["short", "both"]))
                .order_by(Topic.score.desc()).limit(1))
            if topic:
                return topic, want
        raise ValueError("no scored topic available: run `ytagent trends run` first")


def _fill_subjects(script: ShortScript, person: str) -> None:
    """Older scripts have no per-beat subject: for a person's story, the beats up to their quote are about them."""
    if not person or any(b.subject for b in script.beats):
        return
    quote_at = next((i for i, b in enumerate(script.beats) if '"' in b.narration), len(script.beats) - 1)
    for b in script.beats[: quote_at + 1]:
        b.subject = person


def _subject_visual(subject: str, scene: str, seconds: float, used: set[str], credits: list[str],
                    portrait: bool) -> tuple[Visual | None, bool]:
    """Real footage of the named subject: a portrait to open on, else archive film, else a real photo.

    Returns (visual, fit) where fit=True shows the whole (4:3 archive) frame over a blurred background.
    """
    if not subject:
        return None, False
    if not portrait:
        clip = find_archive_clip(subject, scene, seconds, used, credits)
        if clip:
            return Visual(clip, still=False), True
    return find_visual(scene, seconds, used, credits, person=subject), False


def _person(topic: Topic) -> str:
    """For motivation topics titled '<person> on <theme>'."""
    return topic.title.rsplit(" on ", 1)[0] if " on " in topic.title else ""


def make_short(topic_id: int | None = None, upload: bool | None = None, fmt: str | None = None,
               fresh: bool = False) -> dict:
    """`fresh` ignores scripts saved by earlier attempts on the same topic."""
    settings = get_settings()
    topic, fmt = pick_topic(topic_id, fmt)
    style = settings.formats.get(fmt)
    person = _person(topic) if fmt == "motivation" else ""
    with session_scope() as s:
        video = Video(topic_id=topic.id, format="short", status="rendering")
        s.add(video)
        s.flush()
        video_id = video.id
    work = settings.data_dir / "videos" / f"{video_id:04d}-{_slug(topic.title)}"
    work.mkdir(parents=True, exist_ok=True)
    log.info("short %d: %r -> %s", video_id, topic.title, work)

    try:
        previous = None if fresh else _previous_attempt(work)
        if previous:
            log.info("reusing brief and script from %s", previous.name)
            brief = Brief(topic.title, [Source(**x) for x in json.loads((previous / "brief.json").read_text("utf-8"))])
            saved = json.loads((previous / "script.json").read_text("utf-8"))
            saved.setdefault("hook_visual_query", "")
            for beat in saved.get("beats", []):
                beat.setdefault("subject", "")
            script = ShortScript.model_validate(saved)
        else:
            brief = build_format_brief(topic.title, fmt, person=person, context_urls=topic.urls)
            script = write_short(brief, fmt=fmt, angle=topic.llm.get("angle", ""))
        (work / "brief.json").write_text(json.dumps([asdict(x) for x in brief.sources], indent=2), encoding="utf-8")
        (work / "script.json").write_text(script.model_dump_json(indent=2), encoding="utf-8")

        lines = script.lines()
        narration = narrate(lines, work, voice=style.voice if style else None, speed=style.speed if style else None)
        starts = [0.0] + [start for start, _ in narration.spans[1:]]
        ends = starts[1:] + [narration.duration + 0.35]
        queries = [script.hook_visual_query or script.beats[0].visual_query] + [b.visual_query for b in script.beats]
        on_screen = [""] + [b.on_screen for b in script.beats]
        _fill_subjects(script, person)
        subjects = [person or script.beats[0].subject] + [b.subject for b in script.beats]
        segments, labels, used, credits = [], [], set(), []
        for i, (start, end) in enumerate(zip(starts, ends, strict=True)):
            duration = end - start
            if i == len(lines) - 1:  # outro: branded end card
                card = text_card(OUTRO_CARD, work / "card_outro.png")
                segments.append(Segment(card, duration, still=True))
                continue
            labels.append((start + 0.15, end - 0.1, on_screen[i]))
            # A new shot every ~4 s keeps it feeling like a real edit; extra shots reuse the line's query.
            shots = max(1, math.ceil(duration / SHOT_SECONDS))
            for k in range(shots):
                shot_len = duration / shots
                visual, fit = _subject_visual(subjects[i], queries[i], shot_len, used, credits,
                                              portrait=bool(person) and i == 0 and k == 0)
                if visual is None:
                    visual = find_visual(queries[i], shot_len, used, credits)
                if visual is None and k > 0:  # nothing else relevant: stretch the previous shot instead
                    segments[-1].duration += shot_len
                    continue
                if visual is None:
                    card = text_card(on_screen[i] or script.title, work / f"card_{i:02d}.png", with_mascot=False)
                    segments.append(Segment(card, shot_len, still=True))
                else:
                    segments.append(Segment(visual.path, shot_len, still=visual.still, fit=fit))

        music = pick_music(video_id, style.music if style else "")
        final = render_short(segments, narration.wav, narration.words, work, labels, music)
        problems = qa(final)
        description = full_description(script, brief.sources, credits)
        write_package(work, script, description)

        mode = settings.publishing.mode
        do_upload = upload if upload is not None else mode == "api"
        youtube_id = upload_private(final, script, description) if do_upload and not problems else None
        status = "failed" if problems else "pending_review"
    except Exception:
        with session_scope() as s:
            s.get(Video, video_id).status = "failed"
        raise
    finally:
        for tmp in [*work.glob("seg_*.mp4"), *work.glob("music.*"), work / "video_only.mp4", work / "concat.txt"]:
            tmp.unlink(missing_ok=True)
        shutil.rmtree(work / "fonts", ignore_errors=True)

    with session_scope() as s:
        row = s.get(Video, video_id)
        row.status, row.title, row.description, row.tags = status, script.title, description, script.tags
        row.script, row.youtube_id = script.model_dump(), youtube_id
        row.meta = {"dir": str(work), "duration": narration.duration, "problems": problems,
                    "sources": [x.url for x in brief.sources]}
        s.get(Topic, topic.id).status = "used"
    return {"video_id": video_id, "title": script.title, "dir": str(work), "status": status,
            "duration_s": round(narration.duration, 1), "problems": problems, "youtube_id": youtube_id,
            "final": str(Path(work) / "final.mp4")}
