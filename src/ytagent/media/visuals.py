"""Visuals per shot: Pixabay stock video, then Pixabay photos (Ken Burns), else a branded card.

Pixabay content is free for commercial use without attribution.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

from ytagent import brand
from ytagent.config import get_settings

log = logging.getLogger(__name__)

CACHE_TTL_S = 24 * 3600  # Pixabay asks API clients to cache search results for 24 h
ENDPOINTS = {"video": "https://pixabay.com/api/videos/", "photo": "https://pixabay.com/api/"}


@dataclass
class Visual:
    path: Path
    still: bool


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


def _terms(text: str) -> set[str]:
    return {w.rstrip("s") for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 2 and w not in _STOP}


# Non-photographic looks that break a "real footage" feel, unless the query asks for them.
_UNREAL = {"cartoon", "animation", "animated", "illustration", "drawing", "anime", "cgi", "render", "abstract",
           "nebula", "galaxy", "fantasy", "vector", "clipart", "3d"}


def relevance(query: str, hit: dict) -> int:
    """How many meaningful query words appear in the hit's tags (0 for cartoon/abstract looks not asked for)."""
    tags = _terms(hit.get("tags", ""))
    if (tags & _UNREAL) - _terms(query):
        return 0
    return len(_terms(query) & tags)


def _needed(query: str) -> int:
    """Tag overlap required: 2 when the query has 2+ meaningful words, so one generic word can't match."""
    return min(2, len(_terms(query))) or 1


def _download(url: str, path: Path, attempts: int = 3) -> Path | None:
    """Download with back-off on rate limits; None if it keeps failing (the caller tries another hit)."""
    if path.exists():
        return path
    tmp = path.with_suffix(path.suffix + ".part")
    for attempt in range(attempts):
        try:
            with httpx.stream("GET", url, timeout=120, follow_redirects=True) as resp:
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


def find_clip(query: str, min_seconds: float, used: set[str]) -> Path | None:
    need = _needed(query)
    for q in _queries(query):
        hits = [h for h in search("video", q) if f"v{h['id']}" not in used and relevance(query, h) >= need]
        # Most relevant first; then clips long enough not to loop; then vertical ones.
        hits.sort(key=lambda h: (-relevance(query, h), h.get("duration", 0) < min_seconds,
                                 not any(r.get("height", 0) > r.get("width", 1) for r in h["videos"].values())))
        for hit in hits[:5]:
            rendition = _best_rendition(hit)
            if not rendition:
                continue
            path = _download(rendition["url"], _cache_dir() / f"{hit['id']}_{rendition['height']}.mp4")
            if path:
                used.add(f"v{hit['id']}")
                log.info("clip %s for %r (%s)", hit["id"], query, hit.get("tags"))
                return path
    return None


def find_photo(query: str, used: set[str]) -> Path | None:
    need = _needed(query)
    for q in _queries(query):
        hits = [h for h in search("photo", q) if f"p{h['id']}" not in used and relevance(query, h) >= need]
        # Most relevant first; portrait photos crop better to 9:16.
        hits.sort(key=lambda h: (-relevance(query, h), h.get("imageHeight", 0) < h.get("imageWidth", 1)))
        for hit in hits[:5]:
            url = hit.get("largeImageURL") or hit.get("webformatURL")
            path = _download(url, _cache_dir() / f"photo_{hit['id']}.jpg") if url else None
            if path:
                used.add(f"p{hit['id']}")
                log.info("photo %s for %r (%s)", hit["id"], query, hit.get("tags"))
                return path
    return None


# --- NASA Image and Video Library (public domain; no endorsement may be implied) ---------------------------

_SPACE = {"space", "planet", "star", "moon", "mar", "sun", "earth", "astronaut", "rocket", "nebula", "galaxy",
          "black", "hole", "asteroid", "comet", "orbit", "saturn", "jupiter", "venus", "mercury", "universe",
          "telescope", "satellite", "aurora", "eclipse", "cosmo", "spacecraft", "iss"}


def find_nasa_photo(query: str, used: set[str]) -> Path | None:
    if not _terms(query) & _SPACE:
        return None
    resp = httpx.get("https://images-api.nasa.gov/search", timeout=30,
                     params={"q": query, "media_type": "image", "page_size": 20})
    if resp.status_code >= 400:
        return None
    for item in resp.json().get("collection", {}).get("items", []):
        data = item.get("data", [{}])[0]
        nasa_id = data.get("nasa_id", "")
        if not nasa_id or f"n{nasa_id}" in used:
            continue
        if not _terms(query) & _terms(" ".join([data.get("title", ""), *data.get("keywords", [])])):
            continue
        assets = httpx.get(f"https://images-api.nasa.gov/asset/{nasa_id}", timeout=30)
        urls = [i["href"] for i in assets.json().get("collection", {}).get("items", [])] if assets.is_success else []
        url = next((u for u in urls if "~large." in u), None) or next((u for u in urls if "~orig." in u), None)
        path = _download(url.replace("http://", "https://"), _cache_dir() / f"nasa_{_safe(nasa_id)}.jpg") if url \
            else None
        if path:
            used.add(f"n{nasa_id}")
            log.info("nasa %s for %r (%s)", nasa_id, query, data.get("title"))
            return path
    return None


# --- Wikimedia Commons (real people and places; public domain or CC BY / BY-SA with credit) ------------------

_OK_LICENSES = re.compile(r"^(public domain|pd|cc0|cc by(-sa)? [\d.]+|no restrictions)", re.I)


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text)[:80]


def find_commons_photo(query: str, used: set[str], credits: list[str]) -> Path | None:
    """A freely licensed photo; CC licences add an attribution line to `credits`."""
    resp = httpx.get("https://commons.wikimedia.org/w/api.php", timeout=30,
                     headers={"User-Agent": "yt-agent/0.1 (https://github.com/iPrjtD)"},
                     params={"action": "query", "generator": "search", "gsrsearch": f"{query} filetype:bitmap",
                             "gsrnamespace": 6, "gsrlimit": 10, "prop": "imageinfo",
                             "iiprop": "url|extmetadata|size", "iiurlwidth": 1440, "format": "json",
                             "formatversion": 2})
    if resp.status_code >= 400:
        return None
    pages = sorted(resp.json().get("query", {}).get("pages", []), key=lambda p: p.get("index", 99))
    for page in pages:
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata", {})
        license_name = meta.get("LicenseShortName", {}).get("value", "")
        if f"c{page['pageid']}" in used or not _OK_LICENSES.match(license_name):
            continue
        if info.get("width", 0) < 800 or not _terms(query) & _terms(page["title"]):
            continue
        url = info.get("thumburl") or info.get("url")
        path = _download(url, _cache_dir() / f"commons_{page['pageid']}.jpg") if url else None
        if path:
            used.add(f"c{page['pageid']}")
            artist = re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", "")).strip() or "Unknown"
            if not re.match(r"public domain|pd|cc0|no restrictions", license_name, re.I):
                source = info.get("descriptionurl", "")
                credits.append(f"Photo: {artist}, {license_name}, via Wikimedia Commons ({source})")
            log.info("commons %s for %r (%s, %s)", page["title"], query, license_name, artist[:40])
            return path
    return None


def find_visual(query: str, min_seconds: float, used: set[str], credits: list[str] | None = None,
                person: str = "") -> Visual | None:
    """Best real visual for a shot: the person's photo if asked, stock video, NASA, then photos."""
    credits = credits if credits is not None else []
    if person:
        photo = find_commons_photo(person, used, credits)
        if photo:
            return Visual(photo, still=True)
    clip = find_clip(query, min_seconds, used)
    if clip:
        return Visual(clip, still=False)
    for finder in (lambda: find_nasa_photo(query, used), lambda: find_photo(query, used),
                   lambda: find_commons_photo(query, used, credits)):
        photo = finder()
        if photo:
            return Visual(photo, still=True)
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
