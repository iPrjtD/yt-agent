"""Visuals per shot, picked by what the footage actually shows.

Candidates come from Pixabay video and photos (free commercial use, no attribution), the NASA library
(public domain) and Wikimedia Commons (public domain or CC BY / BY-SA with a credit line). A local CLIP model
scores each candidate's small preview against the shot description; only the best match above a quality bar
is downloaded. Nothing good enough -> the caller uses a branded card.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import subprocess
import textwrap
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

from ytagent import brand
from ytagent.config import get_settings
from ytagent.proc import NO_WINDOW
from ytagent.trends.base import USER_AGENT

log = logging.getLogger(__name__)

CACHE_TTL_S = 24 * 3600  # Pixabay asks API clients to cache search results for 24 h
ENDPOINTS = {"video": "https://pixabay.com/api/videos/", "photo": "https://pixabay.com/api/"}
MIN_MATCH = 0.24      # CLIP cosine: ~0.27-0.31 clear match, <=0.20 unrelated (calibrated on Pixabay previews)
MIN_SUBJECT_MATCH = 0.17  # Commons images whose file title names the subject (CLIP may not know the person)
VIDEO_BONUS = 0.015   # prefer moving footage over a photo when they match about equally
HEADERS = {"User-Agent": USER_AGENT}  # Wikimedia rejects requests without a descriptive User-Agent


@dataclass
class Visual:
    path: Path
    still: bool


@dataclass
class Candidate:
    key: str                         # unique id across sources, e.g. "v123" (video), "p45" (photo)
    preview: str                     # small image URL used for matching
    fetch: Callable[[], Path | None]
    still: bool
    credit: str = ""                 # attribution line when the licence needs one
    note: str = ""
    score: float = field(default=0.0)
    preview_video: str = ""          # small video to grab a mid-clip frame from (thumbnails can hide animations)
    duration: float = 0.0


def _cache_dir() -> Path:
    path = get_settings().data_dir / "cache" / "pixabay"
    path.mkdir(parents=True, exist_ok=True)
    return path


def search(kind: str, query: str) -> list[dict]:
    key = get_settings().env.pixabay_api_key
    if not key:
        return []
    cache = _cache_dir() / f"{kind}_{hashlib.sha1(query.encode()).hexdigest()[:16]}.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < CACHE_TTL_S:
        return json.loads(cache.read_text(encoding="utf-8"))
    params = {"key": key, "q": query[:100], "safesearch": "true", "per_page": 30, "order": "popular"}
    if kind == "photo":
        params["image_type"] = "photo"
    for attempt in range(3):
        resp = httpx.get(ENDPOINTS[kind], timeout=30, params=params)
        if resp.status_code != 429:
            break
        time.sleep(5 * (attempt + 1))  # API limit is ~100 requests per minute
    if resp.status_code >= 400:
        log.warning("pixabay %s search %r failed: HTTP %s", kind, query, resp.status_code)
        return []
    hits = resp.json().get("hits", [])
    cache.write_text(json.dumps(hits), encoding="utf-8")
    return hits


def _best_rendition(hit: dict) -> dict | None:
    """Smallest rendition that still covers a 1080x1920 crop reasonably (>=1080 px tall), else the largest."""
    renditions = [r for r in hit.get("videos", {}).values() if r.get("url")]
    good = [r for r in renditions if r["height"] >= 1080 and r["width"] * r["height"] <= 1920 * 1080 * 1.1]
    if good:
        return min(good, key=lambda r: r["width"] * r["height"])
    return max(renditions, key=lambda r: r["width"] * r["height"], default=None)


_STOP = {"with", "the", "and", "for", "from", "into", "onto", "over", "under", "looking", "showing", "of", "in",
         "on", "at", "a", "an", "to", "is", "are", "people", "person", "man", "woman", "outside", "carefully"}

# Non-photographic looks that break a "real footage" feel, unless the query asks for them.
_UNREAL = {"cartoon", "animation", "animated", "illustration", "drawing", "anime", "cgi", "render", "abstract",
           "nebula", "galaxy", "fantasy", "vector", "clipart", "3d", "painting", "sketch", "generated", "art"}


def _terms(text: str) -> set[str]:
    return {w.rstrip("s") for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 2 and w not in _STOP}


# Words that name a kind of thing rather than the thing itself ("Mount" Roraima, saffron "flower").
_GENERIC = {"mount", "mountain", "lake", "river", "city", "island", "temple", "fort", "palace", "park",
            "national", "valley", "desert", "beach", "flower", "plant", "tree", "food", "dish", "spice",
            "street", "old", "ancient", "great", "new", "falls", "fall", "sea", "ocean", "bay", "church",
            "tower", "bridge", "museum", "statue", "cave", "forest", "village", "state", "kingdom", "empire"}


def key_terms(subject: str) -> set[str]:
    """The distinctive words of a name: {"roraima"} for "Mount Roraima", {"saffron", "crocus"} for "saffron crocus"."""
    terms = _terms(subject)
    return (terms - _GENERIC) or terms


def relevance(query: str, hit: dict) -> int:
    """How many meaningful query words appear in the hit's tags (0 for anything tagged cartoon/abstract/3D)."""
    raw = hit.get("tags", "").lower()
    if set(re.findall(r"[a-z0-9]+", raw)) & _UNREAL:  # includes short tags such as "3d"
        return 0
    return len(_terms(query) & _terms(raw))


_PHOTO_HOST = "pixabay.com/get/"  # full-size Pixabay photos: strictly rate-limited
_photo_pause_until = 0.0


def _download(url: str, path: Path, attempts: int = 3) -> Path | None:
    """Download with back-off; None if it keeps failing (the caller tries another candidate).

    A 429 from Pixabay's photo host pauses photo downloads for 2 minutes instead of retrying.
    """
    global _photo_pause_until
    if path.exists():
        return path
    tmp = path.with_suffix(path.suffix + ".part")
    for attempt in range(attempts):
        try:
            with httpx.stream("GET", url, timeout=120, follow_redirects=True, headers=HEADERS) as resp:
                resp.raise_for_status()
                with tmp.open("wb") as fh:
                    for chunk in resp.iter_bytes():
                        fh.write(chunk)
            tmp.replace(path)
            return path
        except httpx.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 429 and _PHOTO_HOST in url:
                _photo_pause_until = time.time() + 120
                log.info("pixabay photo host rate-limited; using videos/other sources for 2 min")
                break
            log.warning("download failed (%s), attempt %d: %s", url[-40:], attempt + 1, exc)
            time.sleep(3 * (attempt + 1))
    tmp.unlink(missing_ok=True)
    return None


def _queries(query: str) -> list[str]:
    words = [w for w in query.split() if w.lower() not in _STOP]
    return [q for q in dict.fromkeys([query, " ".join(words[:2]), " ".join(words[-2:])]) if q]


# --- candidate sources ---------------------------------------------------------------------------------------


def pixabay_candidates(query: str, used: set[str], limit: int = 10) -> list[Candidate]:
    out: list[Candidate] = []
    kinds = [("video", "v")] + ([("photo", "p")] if time.time() >= _photo_pause_until else [])
    for kind, prefix in kinds:
        seen: dict[str, dict] = {}
        for q in _queries(query):
            for hit in search(kind, q):
                k = f"{prefix}{hit['id']}"
                if kind == "video" and hit.get("type") == "animation":  # Pixabay labels clips "film" or "animation"
                    continue
                if k not in used and k not in seen and relevance(query, hit) >= 1:
                    seen[k] = hit
            if len(seen) >= limit:
                break
        ranked = sorted(seen.items(), key=lambda kv: -relevance(query, kv[1]))[:limit]
        for k, hit in ranked:
            if kind == "video":
                rendition = _best_rendition(hit)
                videos = hit["videos"]
                thumb = videos.get("tiny", {}).get("thumbnail") or videos.get("small", {}).get("thumbnail")
                if not rendition or not thumb:
                    continue
                path = _cache_dir() / f"{hit['id']}_{rendition['height']}.mp4"
                out.append(Candidate(k, thumb, lambda u=rendition["url"], p=path: _download(u, p), still=False,
                                     note=hit.get("tags", ""), preview_video=videos.get("tiny", {}).get("url", ""),
                                     duration=float(hit.get("duration") or 0)))
            else:
                url = hit.get("largeImageURL") or hit.get("webformatURL")
                if not url or not hit.get("previewURL"):
                    continue
                path = _cache_dir() / f"photo_{hit['id']}.jpg"
                out.append(Candidate(k, hit["previewURL"], lambda u=url, p=path: _download(u, p), still=True,
                                     note=hit.get("tags", "")))
    return out


_SPACE = {"space", "planet", "star", "moon", "mar", "sun", "earth", "astronaut", "rocket", "nebula", "galaxy",
          "black", "hole", "asteroid", "comet", "orbit", "saturn", "jupiter", "venus", "mercury", "universe",
          "telescope", "satellite", "aurora", "eclipse", "cosmo", "spacecraft", "iss"}


def nasa_candidates(query: str, used: set[str], limit: int = 6) -> list[Candidate]:
    """NASA Image Library (public domain; must not imply NASA endorsement). Only for space-related shots."""
    if not _terms(query) & _SPACE:
        return []
    try:
        resp = httpx.get("https://images-api.nasa.gov/search", timeout=30,
                         params={"q": query, "media_type": "image", "page_size": 30})
        resp.raise_for_status()
    except httpx.HTTPError:
        return []
    out = []
    for item in resp.json().get("collection", {}).get("items", []):
        data = (item.get("data") or [{}])[0]
        nasa_id = data.get("nasa_id", "")
        thumb = next((link["href"] for link in item.get("links", []) if link.get("render") == "image"), "")
        if not nasa_id or not thumb or f"n{nasa_id}" in used:
            continue

        def fetch(nid: str = nasa_id) -> Path | None:
            assets = httpx.get(f"https://images-api.nasa.gov/asset/{nid}", timeout=30)
            items = assets.json().get("collection", {}).get("items", []) if assets.is_success else []
            urls = [i["href"] for i in items]
            url = next((u for u in urls if "~large." in u), None) or next((u for u in urls if "~orig." in u), None)
            safe = re.sub(r"[^A-Za-z0-9_-]+", "_", nid)[:80]
            return _download(url.replace("http://", "https://"), _cache_dir() / f"nasa_{safe}.jpg") if url else None

        out.append(Candidate(f"n{nasa_id}", thumb.replace("http://", "https://"), fetch, still=True,
                             note=data.get("title", "")))
        if len(out) >= limit:
            break
    return out


_OK_LICENSES = re.compile(r"^(public domain|pd|cc0|cc by(-sa)? [\d.]+|no restrictions)", re.I)
_NO_CREDIT = re.compile(r"^(public domain|pd|cc0|no restrictions)", re.I)


def commons_candidates(query: str, used: set[str], limit: int = 6, require_title: bool = False,
                       search: str = "") -> list[Candidate]:
    """Wikimedia Commons photos with a reusable licence; CC licences carry a credit line.

    `require_title`: the file name must mention the query's distinctive words (for named subjects that CLIP
    can't recognise). `search` overrides the search text (e.g. subject + scene words).
    """
    try:
        resp = httpx.get("https://commons.wikimedia.org/w/api.php", timeout=30, headers=HEADERS, params={
            "action": "query", "generator": "search", "gsrsearch": f"{search or query} filetype:bitmap",
            "gsrnamespace": 6, "gsrlimit": 20, "prop": "imageinfo", "iiprop": "url|extmetadata|size",
            "iiurlwidth": 1920, "format": "json", "formatversion": 2})
        resp.raise_for_status()
    except httpx.HTTPError:
        return []
    out = []
    for page in sorted(resp.json().get("query", {}).get("pages", []), key=lambda p: p.get("index", 99)):
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata", {})
        licence = meta.get("LicenseShortName", {}).get("value", "")
        thumb = info.get("thumburl", "")
        if f"c{page['pageid']}" in used or not _OK_LICENSES.match(licence) or info.get("width", 0) < 800 or not thumb:
            continue
        if require_title and not key_terms(query) & _terms(page["title"]):
            continue
        artist = re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", "")).strip() or "Unknown"
        credit = "" if _NO_CREDIT.match(licence) else \
            f"Photo: {artist}, {licence}, via Wikimedia Commons ({info.get('descriptionurl', '')})"
        path = _cache_dir() / f"commons_{page['pageid']}.jpg"
        out.append(Candidate(f"c{page['pageid']}", re.sub(r"/\d+px-", "/330px-", thumb),
                             lambda u=thumb, p=path: _download(u, p), still=True, credit=credit, note=page["title"]))
        if len(out) >= limit:
            break
    return out


# --- selection -----------------------------------------------------------------------------------------------


def _preview(url: str) -> Image.Image | None:
    try:
        resp = httpx.get(url, timeout=20, headers=HEADERS, follow_redirects=True)
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content)).convert("RGB")
    except Exception:  # unreadable preview: just skip the candidate
        return None


def _mid_frame(cand: Candidate) -> Image.Image | None:
    """A frame from the middle of a stock clip (its small preview file), falling back to the thumbnail."""
    if cand.preview_video and cand.duration > 1:
        cmd = [get_settings().env.ffmpeg_binary, "-hide_banner", "-loglevel", "error", "-ss",
               f"{cand.duration / 2:.2f}", "-i", cand.preview_video, "-frames:v", "1", "-f", "image2pipe",
               "-vcodec", "png", "-"]
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=30, creationflags=NO_WINDOW)
            if out.returncode == 0 and out.stdout:
                return Image.open(io.BytesIO(out.stdout)).convert("RGB")
        except (subprocess.TimeoutExpired, OSError):
            pass
    return _preview(cand.preview)


def _blank(img: Image.Image) -> bool:
    """Nearly uniform images (empty backgrounds, solid colours) are useless as footage."""
    import numpy as np

    return float(np.asarray(img.convert("L"), dtype=np.float32).std()) < 18


def rank(candidates: list[Candidate], description: str, real_only: bool = True) -> list[Candidate]:
    """Score candidates by CLIP similarity of their preview to the description; best first.

    With `real_only`, previews CLIP judges to be cartoons / illustrations / 3D renders are dropped (the
    channel's look is real footage, even when a script asks for a "sketch"), as are near-blank images.
    """
    from ytagent.media.clip_match import score_with_realism

    with ThreadPoolExecutor(max_workers=6) as pool:
        previews = list(pool.map(_mid_frame, candidates))
    kept = [(c, img) for c, img in zip(candidates, previews, strict=True) if img is not None and not _blank(img)]
    if not kept:
        return []
    sims, is_real = score_with_realism(f"a photo of {description}", [img for _, img in kept])
    ranked = []
    for (cand, _), sim, real in zip(kept, sims, is_real, strict=True):
        if real_only and not real:
            log.debug("drop %s (looks like a cartoon/render): %s", cand.key, cand.note[:50])
            continue
        cand.score = sim + (0 if cand.still else VIDEO_BONUS)
        ranked.append(cand)
    return sorted(ranked, key=lambda c: c.score, reverse=True)


def find_visual(query: str, min_seconds: float, used: set[str], credits: list[str] | None = None,
                person: str = "", min_match: float | None = None) -> Visual | None:
    """The best-matching real visual for a shot, or None if nothing clears the quality bar
    (`min_match` overrides visuals.min_match, e.g. stricter when an AI scene can be drawn instead)."""
    credits = credits if credits is not None else []
    bar = min_match if min_match is not None else get_settings().visuals.min_match
    if person:  # a real photo of the subject (historical drawings/engravings of them are fine too)
        pool = commons_candidates(person, used, require_title=True)
        description = f"{person}, portrait"
    else:
        pool = pixabay_candidates(query, used) + nasa_candidates(query, used) + commons_candidates(query, used, 4)
        description = query
    for cand in rank(pool, description, real_only=not person):
        # Subject images (title already names the subject; may be drawings/engravings) need a lower bar.
        if cand.score < (MIN_SUBJECT_MATCH if person else bar):
            break
        path = cand.fetch()
        if path:
            used.add(cand.key)
            if cand.credit:
                credits.append(cand.credit)
            log.info("%s %.3f for %r (%s)", cand.key, cand.score, description, cand.note[:60])
            return Visual(path, cand.still)
    log.info("no visual above %.2f for %r", MIN_MATCH, description)
    return None


def find_subject_visual(subject: str, scene: str, min_seconds: float, used: set[str], credits: list[str],
                        portrait: bool = False, scene_bar: float | None = None) -> Visual | None:
    """Real photos or footage OF a named subject (a place, thing, person or event), before look-alike stock.

    Candidates must name the subject's distinctive words: Commons file titles, Pixabay tags. They are ranked
    by how well they also show this shot's scene, e.g. "saffron crocus" + "farmer harvesting flowers".
    `scene_bar`: for shots that did not name the subject, a photo of it is only used when it clearly shows
    the scene on its own (scored against the scene alone, so a saffron photo never stands in for gold bars).
    """
    keys = key_terms(subject)
    pool = commons_candidates(subject, used, limit=10, require_title=True)
    if not portrait:
        scene_words = " ".join(sorted(_terms(scene) - _terms(subject)))[:40]
        if scene_words:  # photos of the subject doing what this shot says, when Commons has them
            seen = {c.key for c in pool}
            pool += [c for c in commons_candidates(subject, used, limit=6, require_title=True,
                                                   search=f"{subject} {scene_words}") if c.key not in seen]
        pool += [c for c in pixabay_candidates(subject, used) if keys & _terms(c.note)]
    description = f"{subject}, portrait" if portrait else scene if scene_bar else f"{subject}, {scene}"
    bar = scene_bar or MIN_SUBJECT_MATCH
    # Titles/tags already name the subject (and may be drawings of historical people), so the bar is lower.
    for cand in rank(pool, description, real_only=False):
        if cand.score < bar:
            break
        if not cand.still and cand.duration and cand.duration < min_seconds:
            continue
        path = cand.fetch()
        if path:
            used.add(cand.key)
            if cand.credit:
                credits.append(cand.credit)
            log.info("subject %s %.3f for %r (%s)", cand.key, cand.score, description, cand.note[:60])
            return Visual(path, cand.still)
    log.info("no subject visual for %r", description)
    return None


def text_card(text: str, out: Path, size: tuple[int, int] = (1080, 1920), with_mascot: bool = True) -> Path:
    """Branded frame: honeycomb background, mascot and a label kept above the caption area."""
    w, h = size
    img = Image.new("RGBA", size, brand.CHARCOAL + (255,))
    brand.honeycomb(img, 70, brand.HEX_LINE, 3)
    img.alpha_composite(brand.radial_glow(size, (w // 2, int(h * 0.30)), 420, brand.HONEY, 70))
    if with_mascot:
        bee = brand.mascot(520)
        img.alpha_composite(bee, ((w - bee.width) // 2, int(h * 0.12)))
    d = ImageDraw.Draw(img)
    f = brand.font("Black", 96)
    y = int(h * 0.44)
    for line in textwrap.wrap(text.upper(), width=14)[:2]:
        tw = d.textlength(line, font=f)
        d.text(((w - tw) / 2, y), line, font=f, fill=brand.HONEY)
        y += 118
    img.convert("RGB").save(out)
    return out
