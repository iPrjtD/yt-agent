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


def find_visual(query: str, min_seconds: float, used: set[str]) -> Visual | None:
    """Best real visual for a shot: a relevant video clip, else a relevant photo."""
    clip = find_clip(query, min_seconds, used)
    if clip:
        return Visual(clip, still=False)
    photo = find_photo(query, used)
    return Visual(photo, still=True) if photo else None


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
