"""End-to-end Short production: topic -> brief -> script -> voice -> visuals -> render -> QA -> package/upload."""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import select

from ytagent.config import get_settings
from ytagent.db import session_scope
from ytagent.media.render import Segment, qa, render_short
from ytagent.media.visuals import find_clip, text_card
from ytagent.media.voice import narrate
from ytagent.models import Topic, Video
from ytagent.publish import full_description, upload_private, write_package
from ytagent.research import build_brief
from ytagent.script import write_short

log = logging.getLogger(__name__)

OUTRO_CARD = "Follow for more buzz"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "short"


def pick_topic(topic_id: int | None) -> Topic:
    with session_scope() as s:
        if topic_id:
            topic = s.get(Topic, topic_id)
            if topic is None:
                raise ValueError(f"topic {topic_id} not found")
            return topic
        topic = s.scalar(
            select(Topic).where(Topic.status == "scored", Topic.llm["best_format"].astext.in_(["short", "both"]))
            .order_by(Topic.score.desc()).limit(1))
        if topic is None:
            raise ValueError("no scored topic available: run `ytagent trends run` first")
        return topic


def make_short(topic_id: int | None = None, upload: bool | None = None) -> dict:
    settings = get_settings()
    topic = pick_topic(topic_id)
    with session_scope() as s:
        video = Video(topic_id=topic.id, format="short", status="rendering")
        s.add(video)
        s.flush()
        video_id = video.id
    work = settings.data_dir / "videos" / f"{video_id:04d}-{_slug(topic.title)}"
    work.mkdir(parents=True, exist_ok=True)
    log.info("short %d: %r -> %s", video_id, topic.title, work)

    try:
        brief = build_brief(topic.title, topic.urls)
        (work / "brief.json").write_text(json.dumps([asdict(x) for x in brief.sources], indent=2), encoding="utf-8")
        script = write_short(brief, angle=topic.llm.get("angle", ""))
        (work / "script.json").write_text(script.model_dump_json(indent=2), encoding="utf-8")

        lines = script.lines()
        narration = narrate(lines, work)
        starts = [0.0] + [start for start, _ in narration.spans[1:]]
        ends = starts[1:] + [narration.duration + 0.35]
        queries = [script.beats[0].visual_query] + [b.visual_query for b in script.beats]
        labels = [""] + [b.on_screen for b in script.beats]
        segments, used = [], set()
        for i, (start, end) in enumerate(zip(starts, ends, strict=True)):
            duration = end - start
            if i == len(lines) - 1:  # outro: branded card
                card = text_card(OUTRO_CARD, work / "card_outro.png")
                segments.append(Segment(card, duration, still=True))
                continue
            clip = find_clip(queries[i], duration, used)
            if clip is None:
                card = text_card(labels[i] or script.title, work / f"card_{i:02d}.png")
                segments.append(Segment(card, duration, still=True))
            else:
                segments.append(Segment(clip, duration, label=labels[i]))

        final = render_short(segments, narration.wav, narration.words, work)
        problems = qa(final)
        description = full_description(script, brief.sources)
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
        for tmp in [*work.glob("seg_*.mp4"), work / "video_only.mp4", work / "concat.txt"]:
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
