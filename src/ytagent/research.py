"""Research brief for a topic: web search (SearXNG) + article text extraction, with sources kept."""

from __future__ import annotations

import logging
import re
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


_WIKI_API = {"wikipedia": "https://en.wikipedia.org/w/api.php", "wikiquote": "https://en.wikiquote.org/w/api.php"}
_NOT_QUOTES = re.compile(r"^==+\s*(Disputed|Misattributed|Quotes about|About|See also|External links|References)",
                         re.I | re.M)


def _wiki_get(client: httpx.Client, site: str, **params) -> dict:
    resp = client.get(_WIKI_API[site], params={"format": "json", "formatversion": 2, **params})
    resp.raise_for_status()
    return resp.json()


def _strip_wikitext(text: str) -> str:
    text = re.sub(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>|<[^>]+>", "", text, flags=re.S)
    text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\{\{[^}]*\}\}", "", text)
    text = re.sub(r"\[https?://\S+\s([^\]]+)\]", r"\1", text)
    text = re.sub(r"\[https?://\S+\]", "", text)
    return text.replace("'''", "").replace("''", "").strip()


def wikiquote_quotes(client: httpx.Client, person: str, limit: int = 15) -> Source | None:
    """Sourced quotes only: top-level '* ' lines before the Disputed/Misattributed/About sections."""
    found = _wiki_get(client, "wikiquote", action="query", list="search", srsearch=person, srlimit=1)
    hits = found.get("query", {}).get("search", [])
    if not hits:
        return None
    title = hits[0]["title"]
    wikitext = _wiki_get(client, "wikiquote", action="parse", page=title, prop="wikitext")["parse"]["wikitext"]
    cut = _NOT_QUOTES.search(wikitext)
    body = wikitext[: cut.start()] if cut else wikitext
    lines = body.splitlines()
    quotes = []
    for i, line in enumerate(lines):
        if line.startswith("* ") and not line.startswith("**"):
            quote = _strip_wikitext(line[2:])
            source = _strip_wikitext(lines[i + 1][3:]) if i + 1 < len(lines) and lines[i + 1].startswith("**") else ""
            if 20 <= len(quote) <= 300:
                quotes.append(f'"{quote}" (source: {source or "see page"})')
        if len(quotes) >= limit:
            break
    if not quotes:
        return None
    url = f"https://en.wikiquote.org/wiki/{title.replace(' ', '_')}"
    return Source(title=f"Wikiquote: {title} (verified quotes)", url=url, text="\n".join(quotes))


def wikipedia_summary(client: httpx.Client, query: str) -> Source | None:
    found = _wiki_get(client, "wikipedia", action="query", list="search", srsearch=query, srlimit=1)
    hits = found.get("query", {}).get("search", [])
    if not hits:
        return None
    title = hits[0]["title"]
    pages = _wiki_get(client, "wikipedia", action="query", prop="extracts", explaintext=1, exchars=2500,
                      titles=title)["query"]["pages"]
    text = pages[0].get("extract", "") if pages else ""
    if len(text) < 300:
        return None
    return Source(title=f"Wikipedia: {title}", url=f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}",
                  text=text)


def build_format_brief(topic: str, fmt: str, person: str = "", context_urls: list[str] | None = None) -> Brief:
    """Format-aware research: verified quotes + biography for motivation, Wikipedia + web for the rest."""
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True) as client:
        if fmt == "motivation" and person:
            brief = Brief(topic=topic)
            for src in (wikiquote_quotes(client, person), wikipedia_summary(client, person)):
                if src:
                    brief.sources.append(src)
            return brief
        wiki = None
        try:
            wiki = wikipedia_summary(client, re.sub(r"^Did you know:\s*", "", topic)[:200])
        except httpx.HTTPError as exc:
            log.info("wikipedia lookup failed: %s", exc)
    brief = build_brief(re.sub(r"^Did you know:\s*", "", topic), context_urls, want=2 if wiki else 3)
    if wiki and all(s.url != wiki.url for s in brief.sources):
        brief.sources.insert(0, wiki)
    return brief


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
