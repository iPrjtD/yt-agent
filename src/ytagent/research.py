"""Research brief for a topic: web search (SearXNG) + article text extraction, with sources kept."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx
import trafilatura

from ytagent.config import get_settings
from ytagent.trends.base import USER_AGENT

log = logging.getLogger(__name__)

# Sites whose pages rarely extract well or are not useful as sources.
_SKIP_DOMAINS = ("youtube.com", "youtu.be", "facebook.com", "instagram.com", "x.com", "twitter.com",
                 "tiktok.com", "pinterest.", "reddit.com", "news.google.com", "linkedin.com")


@dataclass
class Source:
    title: str
    url: str
    text: str


@dataclass
class Brief:
    topic: str
    sources: list[Source] = field(default_factory=list)

    def as_prompt(self, max_chars_per_source: int = 1400) -> str:
        parts = [f"[{i}] {s.title} ({urlparse(s.url).netloc})\n{s.text[:max_chars_per_source]}"
                 for i, s in enumerate(self.sources, 1)]
        return "\n\n".join(parts)


def search(query: str, limit: int = 10) -> list[dict]:
    url = f"{get_settings().env.searxng_url}/search"
    resp = httpx.get(url, params={"q": query, "format": "json", "language": "en", "safesearch": 1}, timeout=30)
    resp.raise_for_status()
    results = [r for r in resp.json().get("results", []) if not any(d in r.get("url", "") for d in _SKIP_DOMAINS)]
    return results[:limit]


def fetch_text(client: httpx.Client, url: str) -> str:
    try:
        resp = client.get(url)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.info("skip %s: %s", url, exc)
        return ""
    return trafilatura.extract(resp.text, include_comments=False, include_tables=False, favor_precision=True) or ""


def build_brief(topic: str, context_urls: list[str] | None = None, want: int = 3) -> Brief:
    """Collect up to `want` readable sources: the topic's own links first, then web search results."""
    brief = Brief(topic=topic)
    candidates = [{"url": u, "title": topic} for u in (context_urls or []) if not any(d in u for d in _SKIP_DOMAINS)]
    try:
        candidates += search(f"{topic} explained") + search(topic)
    except httpx.HTTPError as exc:
        log.warning("search failed: %s", exc)
    seen_domains: set[str] = set()
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True) as client:
        for cand in candidates:
            domain = urlparse(cand["url"]).netloc
            if domain in seen_domains:
                continue
            text = fetch_text(client, cand["url"])
            if len(text) < 600:
                continue
            seen_domains.add(domain)
            brief.sources.append(Source(title=cand.get("title") or domain, url=cand["url"], text=text))
            if len(brief.sources) >= want:
                break
    log.info("brief for %r: %d sources", topic, len(brief.sources))
    return brief
