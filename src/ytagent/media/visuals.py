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
from ytagent.trends.base import USER_AGENT

log = logging.getLogger(__name__)

CACHE_TTL_S = 24 * 3600  # Pixabay asks API clients to cache search results for 24 h
ENDPOINTS = {"video": "https://pixabay.com/api/videos/", "photo": "https://pixabay.com/api/"}
MIN_MATCH = 0.24      # CLIP cosine: ~0.27-0.31 clear match, <=0.20 unrelated (calibrated on Pixabay previews)
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


def relevance(query: str, hit: dict) -> int:
    """How many meaningful query words appear in the hit's tags (0 for cartoon/abstract looks not asked for)."""
    tags = _terms(hit.get("tags", ""))
    if (tags & _UNREAL) - _terms(query):
        return 0
    return len(_terms(query) & tags)


def _download(url: str, path: Path, attempts: int = 3) -> Path | None:
    """Download with back-off on rate limits; None if it keeps failing (the caller tries another candidate)."""
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
    for kind, prefix in (("video", "v"), ("photo", "p")):
        seen: dict[str, dict] = {}
        for q in _queries(query):
            for hit in search(kind, q):
                k = f"{prefix}{hit['id']}"
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
                                     note=hit.get("tags", "")))
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


def commons_candidates(query: str, used: set[str], limit: int = 6) -> list[Candidate]:
    """Wikimedia Commons photos with a reusable licence; CC licences carry a credit line."""
    try:
        resp = httpx.get("https://commons.wikimedia.org/w/api.php", timeout=30, headers=HEADERS, params={
            "action": "query", "generator": "search", "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": 6,
            "gsrlimit": 15, "prop": "imageinfo", "iiprop": "url|extmetadata|size", "iiurlwidth": 1440,
            "format": "json", "formatversion": 2})
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
        artist = re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", "")).strip() or "Unknown"
        credit = "" if _NO_CREDIT.match(licence) else \
            f"Photo: {artist}, {licence}, via Wikimedia Commons ({info.get('descriptionurl', '')})"
        path = _cache_dir() / f"commons_{page['pageid']}.jpg"
        out.append(Candidate(f"c{page['pageid']}", re.sub(r"/\d+px-", "/320px-", thumb),
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


def rank(candidates: list[Candidate], description: str) -> list[Candidate]:
    """Score candidates by CLIP similarity of their preview to the description; best first."""
    from ytagent.media.clip_match import similarity

    with ThreadPoolExecutor(max_workers=6) as pool:
        previews = list(pool.map(_preview, [c.preview for c in candidates]))
    kept = [(c, img) for c, img in zip(candidates, previews, strict=True) if img is not None]
    if not kept:
        return []
    sims = similarity(f"a photo of {description}", [img for _, img in kept])
    for (cand, _), sim in zip(kept, sims, strict=True):
        cand.score = sim + (0 if cand.still else VIDEO_BONUS)
    return sorted((c for c, _ in kept), key=lambda c: c.score, reverse=True)


def find_visual(query: str, min_seconds: float, used: set[str], credits: list[str] | None = None,
                person: str = "") -> Visual | None:
    """The best-matching real visual for a shot, or None if nothing clears the quality bar."""
    credits = credits if credits is not None else []
    if person:  # motivation: a real photo of the person
        pool = commons_candidates(person, used)
        description = f"{person}, portrait"
    else:
        pool = pixabay_candidates(query, used) + nasa_candidates(query, used) + commons_candidates(query, used, 4)
        description = query
    for cand in rank(pool, description):
        if cand.score < MIN_MATCH and not person:
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
