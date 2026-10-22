"""Real archive footage of a named subject (person, film, event) from Wikimedia Commons.

Only files Commons lists as public domain / CC0 / CC BY / CC BY-SA are used (CC licences add a credit line).
For each candidate film, frames are grabbed at several points (FFmpeg seeks inside the remote file, so whole
films are never downloaded), CLIP scores them against the scene, and the best moment is cut out.
"""

from __future__ import annotations

import io
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image

from ytagent.config import get_settings
from ytagent.media.visuals import _NO_CREDIT, _OK_LICENSES, HEADERS, _cache_dir

log = logging.getLogger(__name__)

MIN_ARCHIVE_MATCH = 0.22   # archive film frames are grainy/black-and-white, so CLIP scores run a little lower
OFFSETS_PER_FILM = 8
FILMS_PER_SUBJECT = 4


@dataclass
class Film:
    pageid: int
    title: str
    url: str          # high-quality version used for the final cut
    preview: str      # small transcode used for frame grabs
    duration: float
    credit: str


def _pick(derivatives: list[dict], keys: tuple[str, ...]) -> str:
    by_key = {d.get("transcodekey") or "": d["src"] for d in derivatives}
    return next((by_key[k] for k in keys if k in by_key), "")


def find_films(subject: str) -> list[Film]:
    try:
        resp = httpx.get("https://commons.wikimedia.org/w/api.php", timeout=30, headers=HEADERS, params={
            "action": "query", "generator": "search", "gsrsearch": f"{subject} filetype:video", "gsrnamespace": 6,
            "gsrlimit": 10, "prop": "videoinfo", "viprop": "url|extmetadata|size|derivatives", "format": "json",
            "formatversion": 2})
        resp.raise_for_status()
    except httpx.HTTPError:
        return []
    films = []
    for page in sorted(resp.json().get("query", {}).get("pages", []), key=lambda p: p.get("index", 99)):
        info = (page.get("videoinfo") or [{}])[0]
        meta = info.get("extmetadata", {})
        licence = meta.get("LicenseShortName", {}).get("value", "")
        duration = float(info.get("duration") or 0)
        if not _OK_LICENSES.match(licence) or duration < 8 or not info.get("url"):
            continue
        artist = re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", "")).strip() or "Unknown"
        credit = "" if _NO_CREDIT.match(licence) else \
            f"Footage: {artist}, {licence}, via Wikimedia Commons ({info.get('descriptionurl', '')})"
        ders = info.get("derivatives", [])
        best = _pick(ders, ("1080p.vp9.webm", "720p.vp9.webm", "480p.vp9.webm")) or info["url"]
        small = _pick(ders, ("240p.vp9.webm", "360p.vp9.webm", "360p.mpeg4.mov", "480p.vp9.webm")) or best
        films.append(Film(page["pageid"], page["title"], best, small, duration, credit))
        if len(films) >= FILMS_PER_SUBJECT:
            break
    return films


def _ffmpeg() -> str:
    return get_settings().env.ffmpeg_binary


_FRAMES: dict[tuple[int, int], Image.Image | None] = {}  # (film, second) -> frame, shared by all scenes


def _frame(film: Film, at: float) -> Image.Image | None:
    """One frame at `at` seconds, read straight from the small remote transcode (cached per run).

    Sequential on purpose: Wikimedia's file servers throttle parallel range requests (empty reads).
    """
    key = (film.pageid, int(at))
    if key in _FRAMES:
        return _FRAMES[key]
    cmd = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-user_agent", HEADERS["User-Agent"],
           "-ss", f"{at:.2f}", "-i", film.preview, "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=60)
        img = Image.open(io.BytesIO(out.stdout)).convert("RGB") if out.returncode == 0 and out.stdout else None
    except (subprocess.TimeoutExpired, OSError):
        img = None
    _FRAMES[key] = img
    return img


def _cut(film: Film, start: float, seconds: float) -> Path | None:
    path = _cache_dir() / f"archive_{film.pageid}_{int(start * 10)}_{int(seconds * 10)}.mp4"
    if path.exists() and path.stat().st_size > 10_000:
        return path
    path.unlink(missing_ok=True)  # an earlier failed cut can leave an empty file behind
    cmd = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-user_agent", HEADERS["User-Agent"],
           "-ss", f"{start:.2f}", "-i", film.url, "-t", f"{seconds:.2f}", "-an", "-c:v", "libx264",
           "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(path)]
    out = subprocess.run(cmd, capture_output=True, timeout=300)
    if out.returncode == 0 and path.exists() and path.stat().st_size > 10_000:
        return path
    path.unlink(missing_ok=True)
    return None


def find_archive_clip(subject: str, scene: str, seconds: float, used: set[str],
                      credits: list[str]) -> Path | None:
    """Best matching moment of a public-domain/CC film about `subject`, cut to `seconds`."""
    from ytagent.media.clip_match import similarity

    films = find_films(subject)
    if not films:
        return None
    # Candidate moments spread across each film (skip titles at the start and the end card).
    moments = [(f, f.duration * (0.08 + 0.84 * k / (OFFSETS_PER_FILM - 1))) for f in films
               for k in range(OFFSETS_PER_FILM)]
    moments = [(f, t) for f, t in moments if f"a{f.pageid}@{int(t)}" not in used and t + seconds < f.duration]
    frames = [_frame(film, at) for film, at in moments]
    scored = [(m, img) for m, img in zip(moments, frames, strict=True) if img is not None]
    if not scored:
        return None
    sims = similarity(f"a film still of {subject}, {scene}", [img for _, img in scored])
    # Small penalty for reusing a film already used in this video, to keep variety.
    used_films = {k.split("@")[0] for k in used if k.startswith("a")}
    ranked = sorted(((s - (0.02 if f"a{m[0].pageid}" in used_films else 0), m) for (m, _), s
                     in zip(scored, sims, strict=True)), key=lambda x: x[0], reverse=True)
    for score, (film, start) in ranked:
        if score < MIN_ARCHIVE_MATCH:
            break
        path = _cut(film, start, seconds + 0.3)
        if path:
            used.add(f"a{film.pageid}@{int(start)}")
            if film.credit:
                credits.append(film.credit)
            log.info("archive %s @%.0fs %.3f for %r", film.title[:60], start, score, subject)
            return path
    log.info("no archive moment above %.2f for %r", MIN_ARCHIVE_MATCH, subject)
    return None
