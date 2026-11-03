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
from ytagent.media.visuals import VIDEO_BONUS, Visual, find_subject_visual, find_visual, text_card
from ytagent.media.voice import narrate_lines
from ytagent.models import Topic, Video
from ytagent.providers.images import available as ai_available
from ytagent.providers.images import generate as generate_scene
from ytagent.providers.tts import Line
from ytagent.publish import full_description, upload_private, write_package
from ytagent.research import Brief, Source, build_format_brief
from ytagent.script import MemePlan, ShortScript, plan_meme, write_short

log = logging.getLogger(__name__)

OPENING_VIDEO_BONUS = 0.08  # the first second decides whether people stop scrolling: strongly prefer motion
MIN_MOTION = 0.5            # below this share of moving footage so far, a shot's photo is swapped for a video
RECOMMEND_AT = 65            # quality score from which a Short is added to the "⭐ Recommended" playlist
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


def format_shares() -> dict[str, float]:
    """Target share per format: settings.yaml, adjusted by what performs (data/learned_mix.json from the daily
    `learn` job) when present. Formats with share 0 are not produced."""
    shares = {name: f.share for name, f in get_settings().formats.items()}
    learned = get_settings().data_dir / "learned_mix.json"
    if learned.exists():
        try:
            shares.update({k: float(v) for k, v in json.loads(learned.read_text("utf-8"))["shares"].items()
                           if k in shares})
        except (ValueError, KeyError, TypeError):
            log.warning("ignoring unreadable %s", learned.name)
    return {k: v for k, v in shares.items() if v > 0}


def pick_topic(topic_id: int | None, fmt: str | None = None) -> tuple[Topic, str]:
    """Explicit topic, else the best topic of the format furthest below its target share of recent videos."""
    shares = format_shares()
    with session_scope() as s:
        if topic_id:
            topic = s.get(Topic, topic_id)
            if topic is None:
                raise ValueError(f"topic {topic_id} not found")
            return topic, fmt or _format_of(topic)
        recent = s.scalars(select(Video).where(Video.status != "failed").order_by(Video.id.desc()).limit(20)).all()
        counts = Counter(v.script.get("pillar") for v in recent if v.script)
        total = max(1, sum(counts.values()))
        order = [fmt] if fmt else sorted(shares, key=lambda f: counts[f] / total - shares[f])
        for want in order:
            topic = s.scalar(
                select(Topic).where(Topic.status == "scored", Topic.llm["content_format"].astext == want,
                                    Topic.llm["best_format"].astext.in_(["short", "both"]))
                .order_by(Topic.score.desc()).limit(1))
            if topic:
                return topic, want
        raise ValueError("no scored topic available: run `ytagent trends run` first")


REACTION_SECONDS = 1.6
STRONG_MATCH = 0.27  # CLIP bar for real stock when an AI scene can be drawn instead
HISTORIC = re.compile(r"\b(ancient|medieval|centur(y|ies)|\d{3,4}s|\d+(st|nd|rd|th)[- ]century|bce?|victorian|"
                      r"historic(al)?|empire|king|queen|emperor|pharaoh|maharaja|mughal|vedic|samurai|viking|"
                      r"roman|greek|egyptian|prehistoric|stone age|olden)\b", re.I)


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
        if unit > 0 and (step.meme_top or step.meme_bottom):  # the hook shows its own big text instead
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
        elif found is not None and words[found].start > max(t for t in times if t is not None) + 1.2:
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


class NoFootage(RuntimeError):
    """The opening shot (the hook's promise) has no real moving footage: skip the topic before spending voice
    quota."""


def _shot_visual(shot, subject: str, portrait: bool, seconds: float, used: set[str], credits: list[str],
                 work: Path, main_subject: str = "", real_only: bool = False,
                 video_bonus: float = VIDEO_BONUS, video_only: bool = False) -> tuple[Visual | None, bool, str]:
    """Best visual for one shot: the named subject's real footage, real stock that shows the words,
    else an AI-drawn scene (not with `real_only` or `video_only`). Never an unrelated clip.

    Returns (visual, fit, kind) with kind "real" or "ai" (for the quality score).
    """
    ai_cache = get_settings().data_dir / "cache" / "ai"
    subject = shot.subject.strip() or subject  # the shot's own named subject, else the beat's
    if subject:
        visual, fit = _subject_visual(subject, shot.query, seconds, used, credits, portrait=portrait,
                                      video_bonus=video_bonus, video_only=video_only)
        if visual:
            return visual, fit, "real"
    elif main_subject:  # e.g. "hands sorting spice threads" in a saffron Short: real saffron if it fits
        visual = find_subject_visual(main_subject, shot.query, seconds, used, credits, scene_bar=STRONG_MATCH,
                                     video_bonus=video_bonus, video_only=video_only)
        if visual:
            return visual, False, "real"
    ai_on = ai_available() and not real_only and not video_only
    if ai_on and HISTORIC.search(f"{shot.scene} {shot.query}"):
        # Stock libraries have no footage of ancient/old-era moments: draw the scene instead.
        drawn = generate_scene(shot.scene, ai_cache)
        if drawn:
            return Visual(drawn, still=True), True, "ai"
    # With AI scenes available (or for the opening), real stock must be a strong match; weaker ones get drawn.
    strict = ai_on or real_only
    visual = find_visual(shot.query, seconds, used, credits, min_match=STRONG_MATCH if strict and not video_only
                         else None, video_bonus=video_bonus, video_only=video_only)
    if visual:
        return visual, False, "real"
    if not ai_on:
        return None, False, ""
    drawn = generate_scene(shot.scene, ai_cache)
    return (Visual(drawn, still=True), True, "ai") if drawn else (None, False, "")


def _opening_visual(plan: MemePlan, script: ShortScript, subject: str, main: str, used: set[str],
                    credits: list[str], work: Path) -> tuple[Visual, bool]:
    """Moving real footage for the first shot: of the shot itself, else of the hook's visual idea, else of the
    Short's main subject. Raises NoFootage when there is none (the topic is skipped)."""
    shot = plan.hook.shots[0]
    tries = [shot] + [shot.model_copy(update={"query": q, "subject": ""})
                      for q in dict.fromkeys((script.hook_visual_query, main)) if q and q != shot.query]
    for k, attempt in enumerate(tries):
        visual, fit, _ = _shot_visual(attempt, subject if k == 0 else "", portrait=False, seconds=3.0,
                                      used=used, credits=credits, work=work, main_subject=main,
                                      real_only=True, video_bonus=OPENING_VIDEO_BONUS, video_only=True)
        if visual is not None:
            return visual, fit
    raise NoFootage(f"no real moving footage for the opening shot {shot.query!r}")


def _motion(parts: list[tuple[bool, str, float]]) -> float:
    """Share of the Short so far that is moving footage (1.0 before the first shot)."""
    total = sum(sec for *_, sec in parts)
    return sum(sec for still, _, sec in parts if not still) / total if total else 1.0


def quality(parts: list[tuple[bool, str, float]], duration: float, niche_fit: float) -> dict:
    """0-100 score from what the data says keeps viewers: a moving opening, motion throughout, real footage,
    26-33 s length and topic fit. `parts` are (still, kind, seconds) per segment."""
    total = sum(sec for *_, sec in parts) or 1.0
    opening = 20 if parts and not parts[0][0] else 8 if parts and parts[0][1] == "real" else 0
    motion = 30 * sum(sec for still, _, sec in parts if not still) / total
    real = 20 * sum(sec for _, kind, sec in parts if kind == "real") / total
    length = 10 if 24 <= duration <= 34 else 5 if duration <= 40 else 0
    fit = 20 * max(0.0, min(1.0, niche_fit / 10))
    score = round(opening + motion + real + length + fit)
    return {"score": score, "opening_moves": bool(parts and not parts[0][0]), "motion": round(motion / 30, 2),
            "real": round(real / 20, 2), "duration": round(duration, 1), "niche_fit": niche_fit}


def main_subject(plan: MemePlan) -> str:
    """The subject most shots name (the thing or place the Short is about), if at least two do."""
    names = Counter(s.subject.strip() for step in [plan.hook, *plan.beats] for s in step.shots if s.subject.strip())
    name, count = names.most_common(1)[0] if names else ("", 0)
    return name if count >= 2 else ""


def _fill_subjects(script: ShortScript, person: str) -> None:
    """Older scripts have no per-beat subject: for a person's story, the beats up to their quote are about them."""
    if not person or any(b.subject for b in script.beats):
        return
    quote_at = next((i for i, b in enumerate(script.beats) if '"' in b.narration), len(script.beats) - 1)
    for b in script.beats[: quote_at + 1]:
        b.subject = person


def _subject_visual(subject: str, scene: str, seconds: float, used: set[str], credits: list[str],
                    portrait: bool, video_bonus: float = VIDEO_BONUS,
                    video_only: bool = False) -> tuple[Visual | None, bool]:
    """Real footage of the named subject: a portrait to open on, else archive film, else real photos/stock of it.

    Returns (visual, fit) where fit=True shows the whole (4:3 archive) frame over a blurred background.
    """
    if not subject:
        return None, False
    if not portrait:
        clip = find_archive_clip(subject, scene, seconds, used, credits)
        if clip:
            return Visual(clip, still=False), True
    return find_subject_visual(subject, scene, seconds, used, credits, portrait=portrait,
                               video_bonus=video_bonus, video_only=video_only), False


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
            saved.setdefault("hook_text", "")
            saved.setdefault("comment_question", "")
            for beat in saved.get("beats", []):
                beat.setdefault("subject", "")
            script = ShortScript.model_validate(saved)
        else:
            brief = build_format_brief(topic.title, fmt, person=person, context_urls=topic.urls)
            script = write_short(brief, fmt=fmt, angle=topic.llm.get("angle", ""))
        (work / "brief.json").write_text(json.dumps([asdict(x) for x in brief.sources], indent=2), encoding="utf-8")
        (work / "script.json").write_text(script.model_dump_json(indent=2), encoding="utf-8")

        log.info("script ready: %r; planning shots", script.title)
        plan = _load_or_plan(previous, work, script, fmt)
        _fill_subjects(script, person)
        subjects = [person or script.beats[0].subject] + [b.subject for b in script.beats]
        main = main_subject(plan)
        used, credits = set(), []
        # The opening must be real MOVING footage of what the hook promises: check before narrating.
        opening, opening_fit = _opening_visual(plan, script, subjects[0], main, used, credits, work)
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
        log.info("narrating %d lines", len(tts_lines))
        narration = narrate_lines(tts_lines, work, style)
        log.info("narration %.1fs; finding visuals", narration.duration)
        first_line = [unit_of_line.index(u) for u in range(n_units)]
        starts = [0.0] + [narration.spans[first_line[u]][0] for u in range(1, n_units)]
        ends = starts[1:] + [narration.duration + 0.35]
        on_screen = [""] + [b.on_screen for b in script.beats]
        steps = [plan.hook, *plan.beats]
        segments, labels, parts = [], [], []
        for i, (start, end) in enumerate(zip(starts, ends, strict=True)):
            if i == n_units - 1:  # outro: the opening footage again, so the replay loops seamlessly
                segments.append(Segment(opening.path, end - start, still=opening.still, fit=opening_fit))
                parts.append((opening.still, "real", end - start))
                continue
            labels.append((start + 0.15, end - 0.1, on_screen[i]))
            # One shot per spoken phrase, cut when its first words are heard.
            unit_lines = {n for n, u in enumerate(unit_of_line) if u == i}
            unit_words = [w for w in narration.words if w.line in unit_lines]
            shots = steps[i].shots
            cuts = _shot_times(shots, unit_words, start, end)
            for k, cut in enumerate(cuts):
                seconds = (cuts[k + 1] if k + 1 < len(cuts) else end) - cut
                if i == 0 and k == 0:
                    visual, fit, kind = opening, opening_fit, "real"
                else:
                    visual, fit, kind = _shot_visual(shots[k], subjects[i], portrait=False, seconds=seconds,
                                                     used=used, credits=credits, work=work, main_subject=main)
                    if (visual is None or visual.still) and _motion(parts) < MIN_MOTION:
                        # Too many photos so far: real video of the topic in general beats another still.
                        moving, mfit, mkind = _shot_visual(shots[k], subjects[i], portrait=False, seconds=seconds,
                                                           used=used, credits=credits, work=work,
                                                           main_subject=main, video_only=True)
                        if moving is not None:
                            visual, fit, kind = moving, mfit, mkind
                if visual is None and segments and k > 0:  # nothing fits: hold the previous shot longer
                    segments[-1].duration += seconds
                    parts[-1] = (*parts[-1][:2], parts[-1][2] + seconds)
                    continue
                if visual is None:
                    card = text_card(on_screen[i] or script.title, work / f"card_{i:02d}_{k}.png", with_mascot=False)
                    segments.append(Segment(card, seconds, still=True))
                    parts.append((True, "card", seconds))
                else:
                    segments.append(Segment(visual.path, seconds, still=visual.still, fit=fit))
                    parts.append((visual.still, kind, seconds))

        log.info("%d segments; rendering", len(segments))
        memes, reactions, sfx, host_dir = _meme_layers(plan, narration, starts, ends, work)
        buzz_lines = frozenset(i for i, ln in enumerate(tts_lines) if ln.speaker == "Buzzbee")
        music = pick_music(video_id, style.music if style else "")
        hook_text = (0.0, min(ends[0], 2.6), script.hook_text)
        follow = (starts[-1], ends[-1] - 0.1)
        final = render_short(segments, narration.wav, narration.words, work, labels, music,
                             memes=memes, buzz_lines=buzz_lines, reactions=reactions, sfx=sfx, host_dir=host_dir,
                             hook=hook_text, follow=follow)
        problems = qa(final, narration.duration)
        score = quality(parts, narration.duration, float(topic.llm.get("niche_fit") or 5))
        log.info("quality %s", score)
        description = full_description(script, brief.sources, credits)
        write_package(work, script, description)

        mode = settings.publishing.mode
        do_upload = upload if upload is not None else mode == "api"
        recommended = score["score"] >= RECOMMEND_AT
        youtube_id = (upload_private(final, script, description, recommended=recommended)
                      if do_upload and not problems else None)
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
                    "sources": [x.url for x in brief.sources], "quality": score, "recommended": recommended}
        s.get(Topic, topic.id).status = "used"
    return {"video_id": video_id, "title": script.title, "dir": str(work), "status": status,
            "duration_s": round(narration.duration, 1), "problems": problems, "youtube_id": youtube_id,
            "quality": score["score"], "recommended": recommended,
            "final": str(Path(work) / "final.mp4")}
