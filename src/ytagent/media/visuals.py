"""Visuals per beat: Pixabay stock video (free for commercial use), else a branded text card."""

from __future__ import annotations

import hashlib
import json
import logging
import textwrap
import time
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

from ytagent import brand
from ytagent.config import get_settings

log = logging.getLogger(__name__)

CACHE_TTL_S = 24 * 3600  # Pixabay asks API clients to cache search results for 24 h


def _cache_dir() -> Path:
    path = get_settings().data_dir / "cache" / "pixabay"
    path.mkdir(parents=True, exist_ok=True)
    return path


def search_videos(query: str) -> list[dict]:
    key = get_settings().env.pixabay_api_key
    if not key:
        return []
    cache = _cache_dir() / f"q_{hashlib.sha1(query.encode()).hexdigest()[:16]}.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < CACHE_TTL_S:
        return json.loads(cache.read_text(encoding="utf-8"))
    resp = httpx.get("https://pixabay.com/api/videos/", timeout=30, params={
        "key": key, "q": query[:100], "safesearch": "true", "per_page": 20, "order": "popular"})
    resp.raise_for_status()
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


def find_clip(query: str, min_seconds: float, used: set[int]) -> Path | None:
    """Download a fitting clip for `query`, trying shorter queries if nothing matches."""
    words = query.split()
    for q in dict.fromkeys([query, " ".join(words[:2]), words[0] if words else ""]):
        if not q:
            continue
        hits = [h for h in search_videos(q) if h["id"] not in used]
        # Prefer vertical clips and clips long enough not to loop.
        hits.sort(key=lambda h: (h.get("duration", 0) < min_seconds,
                                 not any(r.get("height", 0) > r.get("width", 1) for r in h["videos"].values())))
        for hit in hits[:5]:
            rendition = _best_rendition(hit)
            if not rendition:
                continue
            path = _cache_dir() / f"{hit['id']}_{rendition['height']}.mp4"
            if not path.exists():
                with httpx.stream("GET", rendition["url"], timeout=120, follow_redirects=True) as resp:
                    resp.raise_for_status()
                    with path.open("wb") as fh:
                        for chunk in resp.iter_bytes():
                            fh.write(chunk)
            used.add(hit["id"])
            log.info("clip %s for %r (%s)", hit["id"], q, hit.get("tags"))
            return path
    return None


def text_card(text: str, out: Path, size: tuple[int, int] = (1080, 1920), with_mascot: bool = True) -> Path:
    """Branded fallback frame: honeycomb background, mascot and a big label."""
    w, h = size
    img = Image.new("RGBA", size, brand.CHARCOAL + (255,))
    brand.honeycomb(img, 70, brand.HEX_LINE, 3)
    img.alpha_composite(brand.radial_glow(size, (w // 2, int(h * 0.42)), 420, brand.HONEY, 70))
    if with_mascot:
        bee = brand.mascot(560)
        img.alpha_composite(bee, ((w - bee.width) // 2, int(h * 0.20)))
    d = ImageDraw.Draw(img)
    f = brand.font("Black", 96)
    lines = textwrap.wrap(text.upper(), width=14)[:3]
    y = int(h * 0.56)
    for line in lines:
        tw = d.textlength(line, font=f)
        d.text(((w - tw) / 2, y), line, font=f, fill=brand.HONEY)
        y += 118
    img.convert("RGB").save(out)
    return out
