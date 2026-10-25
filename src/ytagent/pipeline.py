"""End-to-end Short production: topic -> brief -> script -> voice -> visuals -> render -> QA -> package/upload."""

from __future__ import annotations

import json
import logging
import re
import shutil
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select

from ytagent.config import get_settings
from ytagent.db import session_scope
from ytagent.media.archive import find_archive_clip
from ytagent.media.reactions import Reaction, loudness_envelope, render_frames, render_host_frames
from ytagent.media.render import Segment, pick_music, qa, render_short
from ytagent.media.sfx import sfx_file
from ytagent.media.visuals import Visual, find_visual, text_card
from ytagent.media.voice import narrate_lines
from ytagent.models import Topic, Video
from ytagent.providers.images import generate as generate_scene
from ytagent.providers.tts import Line
from ytagent.publish import full_description, upload_private, write_package
from ytagent.research import Brief, Source, build_format_brief
from ytagent.script import MemePlan, ShortScript, plan_meme, write_short

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


REACTION_SECONDS = 1.6


def _load_or_plan(previous: Path | None, work: Path, script: ShortScript, fmt: str) -> MemePlan:
    """Reuse a saved meme plan when resuming, else ask the writer model for one."""
    saved = previous / "plan.json" if previous else None
    plan = None
    if saved and saved.exists():
        try:
            plan = MemePlan.model_validate_json(saved.read_text("utf-8"))
        except ValidationError:  # plan from an older version (e.g. no shot list): plan again
            plan = None
    if plan is None:
        plan = plan_meme(script, fmt)
    (work / "plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    return plan


def _meme_layers(plan: MemePlan, narration, starts: list[float], ends: list[float],
                 work: Path) -> tuple[list, list[Reaction], list[tuple[float, Path]], Path | None]:
    """Meme captions, Buzzbee pop-up reactions, sound effects and the talking corner host."""
    fps = get_settings().video.fps
    cache = get_settings().data_dir / "cache" / "sfx"
    memes, reactions, sfx = [], [], []
    for unit, step in enumerate([plan.hook, *plan.beats]):
        start, end = starts[unit], ends[unit]
        if step.meme_top or step.meme_bottom:
            memes.append((start + 0.1, min(end - 0.1, start + 3.2), step.meme_top, step.meme_bottom))
        if step.sfx != "none":
            sfx.append((max(0.0, start - 0.05), sfx_file(step.sfx, cache)))
        if step.reaction != "none" and end - (start + 0.5) >= 0.8:
            at = start + 0.5
            reactions.append(Reaction(at, min(REACTION_SECONDS, end - 0.1 - at), step.reaction))
            sfx.append((at, sfx_file("pop", cache)))
    for j, r in enumerate(sorted(reactions, key=lambda r: r.start)):
        render_frames(r, work / "reactions" / f"r{j:02d}", fps)
    host_dir = None
    if narration.samples is not None:  # Buzzbee in the corner talks along with the whole voice track
        envelope = loudness_envelope(narration.samples, narration.sr, 0.0, narration.duration + 0.4, fps)
        host_dir = render_host_frames(narration.duration + 0.4, envelope, work / "host", fps)
    return memes, reactions, sfx, host_dir


def _shot_times(shots: list, words: list, start: float, end: float) -> list[float]:
    """Start time of each shot: where its first words are spoken in this scene (evenly spread if not found)."""
    norm = [re.sub(r"[^\w%]", "", w.text.lower()) for w in words]
    times, cursor = [], 0
    for k, shot in enumerate(shots):
        cue = [re.sub(r"[^\w%]", "", t.lower()) for t in shot.words.split()[:2] if t.strip()]
        found = None
        for i in range(cursor, len(norm)):
            if cue and norm[i:i + len(cue)] == cue:
                found = i
                break
        if k == 0:
            times.append(start)
        elif found is not None and words[found].start > times[-1] + 1.2:
            times.append(words[found].start)
            cursor = found + 1
        else:
            times.append(None)
    # Fill unknown cut points evenly between their neighbours.
    for k, t in enumerate(times):
        if t is None:
            nxt = next((x for x in times[k + 1:] if x is not None), end)
            gap = len([x for x in times[k:] if x is None]) + 1
            times[k] = times[k - 1] + (nxt - times[k - 1]) / gap
    return [t for t in times if t < end - 0.8] or [start]


def _shot_visual(shot, subject: str, portrait: bool, seconds: float, used: set[str], credits: list[str],
                 work: Path) -> tuple[Visual | None, bool]:
    """Best visual for one shot: the named subject's real footage, real stock that shows the words,
    else an AI-drawn scene. Never an unrelated clip."""
    if subject:
        visual, fit = _subject_visual(subject, shot.query, seconds, used, credits, portrait=portrait)
        if visual:
            return visual, fit
    visual = find_visual(shot.query, seconds, used, credits)
    if visual:
        return visual, False
    drawn = generate_scene(shot.scene, get_settings().data_dir / "cache" / "ai")
    return (Visual(drawn, still=True), True) if drawn else (None, False)


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

        plan = _load_or_plan(previous, work, script, fmt)
        # Spoken lines in order: hook, each beat (+ an optional Buzzbee quip), outro. Units = hook, beats, outro.
        tts_lines: list[Line] = []
        unit_of_line: list[int] = []
        spoken = [(script.hook, plan.hook), *zip((b.narration for b in script.beats), plan.beats, strict=True)]
        for unit, (text, step) in enumerate(spoken):
            tts_lines.append(Line("Narrator", text, step.emotion))
            unit_of_line.append(unit)
            if step.buzz_line:
                tts_lines.append(Line("Buzzbee", step.buzz_line, "playful"))
                unit_of_line.append(unit)
        n_units = len(script.beats) + 2
        tts_lines.append(Line("Narrator", script.outro, plan.outro_emotion))
        unit_of_line.append(n_units - 1)
        narration = narrate_lines(tts_lines, work, style)
        first_line = [unit_of_line.index(u) for u in range(n_units)]
        starts = [0.0] + [narration.spans[first_line[u]][0] for u in range(1, n_units)]
        ends = starts[1:] + [narration.duration + 0.35]
        on_screen = [""] + [b.on_screen for b in script.beats]
        _fill_subjects(script, person)
        subjects = [person or script.beats[0].subject] + [b.subject for b in script.beats]
        steps = [plan.hook, *plan.beats]
        segments, labels, used, credits = [], [], set(), []
        for i, (start, end) in enumerate(zip(starts, ends, strict=True)):
            if i == n_units - 1:  # outro: branded end card
                card = text_card(OUTRO_CARD, work / "card_outro.png")
                segments.append(Segment(card, end - start, still=True))
                continue
            labels.append((start + 0.15, end - 0.1, on_screen[i]))
            # One shot per spoken phrase, cut when its first words are heard.
            unit_lines = {n for n, u in enumerate(unit_of_line) if u == i}
            unit_words = [w for w in narration.words if w.line in unit_lines]
            shots = steps[i].shots
            cuts = _shot_times(shots, unit_words, start, end)
            for k, cut in enumerate(cuts):
                seconds = (cuts[k + 1] if k + 1 < len(cuts) else end) - cut
                visual, fit = _shot_visual(shots[k], subjects[i], portrait=bool(person) and i == 0 and k == 0,
                                           seconds=seconds, used=used, credits=credits, work=work)
                if visual is None and segments and k > 0:  # nothing fits: hold the previous shot longer
                    segments[-1].duration += seconds
                    continue
                if visual is None:
                    card = text_card(on_screen[i] or script.title, work / f"card_{i:02d}_{k}.png", with_mascot=False)
                    segments.append(Segment(card, seconds, still=True))
                else:
                    segments.append(Segment(visual.path, seconds, still=visual.still, fit=fit))

        memes, reactions, sfx, host_dir = _meme_layers(plan, narration, starts, ends, work)
        buzz_lines = frozenset(i for i, ln in enumerate(tts_lines) if ln.speaker == "Buzzbee")
        music = pick_music(video_id, style.music if style else "")
        final = render_short(segments, narration.wav, narration.words, work, labels, music,
                             memes=memes, buzz_lines=buzz_lines, reactions=reactions, sfx=sfx, host_dir=host_dir)
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
        shutil.rmtree(work / "reactions", ignore_errors=True)
        shutil.rmtree(work / "host", ignore_errors=True)

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
