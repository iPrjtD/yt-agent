"""Trend sources. Each has a pure `parse_*` function (unit-tested) and a thin `fetch`."""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser
import httpx

from ytagent.trends.base import TrendItem, TrendSource, log_strength, rank_strength, topic_key

# --- Google Trends (daily trending searches RSS) --------------------------------------------------


def _parse_traffic(text: str) -> int:
    """'2,000+' -> 2000, '50K+' -> 50000, '1M+' -> 1000000."""
    m = re.match(r"\s*([\d.,]+)\s*([KkMm]?)", text or "")
    if not m:
        return 0
    num = float(m.group(1).replace(",", ""))
    return int(num * {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2).lower()])


_HT = "{https://trends.google.com/trending/rss}"


def parse_google_trends(xml: str) -> list[TrendItem]:
    # ElementTree rather than feedparser: feedparser keeps only one of the repeated ht:news_item children.
    items = []
    for node in ET.fromstring(xml).iter("item"):
        title = (node.findtext("title") or "").strip()
        if not title:
            continue
        traffic = _parse_traffic(node.findtext(f"{_HT}approx_traffic") or "")
        news = node.findall(f"{_HT}news_item")
        news_titles = [html.unescape(n.findtext(f"{_HT}news_item_title") or "") for n in news]
        news_urls = [n.findtext(f"{_HT}news_item_url") or "" for n in news]
        items.append(TrendItem(
            source="google_trends",
            external_id=topic_key(title),
            title=title,
            url=news_urls[0] if news_urls else None,
            strength=log_strength(traffic, 1_000_000),
            related=len(news),
            published_at=_parse_date(node.findtext("pubDate")),
            context=" | ".join(t for t in news_titles if t),
            raw={"traffic": traffic, "news_urls": news_urls},
        ))
    return items


@dataclass
class GoogleTrends:
    geo: str
    name: str = "google_trends"

    def fetch(self, client: httpx.Client) -> list[TrendItem]:
        resp = client.get("https://trends.google.com/trending/rss", params={"geo": self.geo})
        resp.raise_for_status()
        return parse_google_trends(resp.text)


# --- Google News (top stories RSS; each item lists related coverage) ------------------------------


def parse_google_news(xml: str) -> list[TrendItem]:
    feed = feedparser.parse(xml)
    total = len(feed.entries)
    items = []
    for rank, entry in enumerate(feed.entries):
        title = re.sub(r"\s+-\s+[^-]+$", "", entry.title)  # drop " - Publisher"
        related = [html.unescape(t) for t in re.findall(r"<a [^>]*>(.*?)</a>", entry.get("summary", ""))]
        coverage = min(1.0, len(related) / 5)
        items.append(TrendItem(
            source="google_news",
            external_id=entry.get("id") or entry.link,
            title=title,
            url=entry.link,
            strength=round(0.5 * rank_strength(rank, total) + 0.5 * coverage, 4),
            related=len(related),
            published_at=_parse_date(entry.get("published")),
            context=" | ".join(related[1:]),
        ))
    return items


@dataclass
class GoogleNews:
    geo: str
    language: str
    name: str = "google_news"

    def fetch(self, client: httpx.Client) -> list[TrendItem]:
        hl = f"{self.language}-{self.geo}"
        resp = client.get("https://news.google.com/rss",
                          params={"hl": hl, "gl": self.geo, "ceid": f"{self.geo}:{self.language}"})
        resp.raise_for_status()
        return parse_google_news(resp.text)


# --- Wikipedia (most viewed articles yesterday) ---------------------------------------------------

_WIKI_SKIP = re.compile(
    r"^(Main_Page|Special:|Wikipedia:|Portal:|File:|Help:|Template:|Category:|Talk:|User:|Draft:|Module:)"
)


def parse_wikipedia_top(data: dict[str, Any], project: str, top_n: int) -> list[TrendItem]:
    articles = data["items"][0]["articles"]
    lang = project.split(".")[0]
    items = []
    for art in articles:
        if _WIKI_SKIP.match(art["article"]) or art["article"] == "-":
            continue
        title = art["article"].replace("_", " ")
        items.append(TrendItem(
            source="wikipedia",
            external_id=art["article"],
            title=title,
            url=f"https://{lang}.wikipedia.org/wiki/{art['article']}",
            strength=log_strength(art["views"], 3_000_000),
            raw={"views": art["views"], "rank": art["rank"]},
        ))
        if len(items) >= top_n:
            break
    return items


@dataclass
class Wikipedia:
    project: str = "en.wikipedia"
    top_n: int = 50
    name: str = "wikipedia"

    def fetch(self, client: httpx.Client) -> list[TrendItem]:
        # Yesterday's list is published a few hours after midnight UTC; fall back one more day.
        for days_back in (1, 2):
            day = datetime.now(UTC) - timedelta(days=days_back)
            url = (f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/{self.project}/all-access/"
                   f"{day:%Y/%m/%d}")
            resp = client.get(url)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            return parse_wikipedia_top(resp.json(), self.project, self.top_n)
        return []


# --- Hacker News (front page via Algolia) ---------------------------------------------------------


def parse_hackernews(data: dict[str, Any], min_points: int) -> list[TrendItem]:
    items = []
    for hit in data.get("hits", []):
        points = hit.get("points") or 0
        if points < min_points:
            continue
        items.append(TrendItem(
            source="hackernews",
            external_id=str(hit["objectID"]),
            title=hit["title"],
            url=hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}",
            strength=min(1.0, points / 1000),
            related=hit.get("num_comments") or 0,
            published_at=datetime.fromtimestamp(hit["created_at_i"], UTC) if hit.get("created_at_i") else None,
            raw={"points": points, "comments": hit.get("num_comments")},
        ))
    return items


@dataclass
class HackerNews:
    min_points: int = 150
    name: str = "hackernews"

    def fetch(self, client: httpx.Client) -> list[TrendItem]:
        resp = client.get("https://hn.algolia.com/api/v1/search", params={"tags": "front_page", "hitsPerPage": 50})
        resp.raise_for_status()
        return parse_hackernews(resp.json(), self.min_points)


# --- YouTube most popular chart (needs YOUTUBE_API_KEY; 1 quota unit per call) ---------------------


def parse_youtube_popular(data: dict[str, Any]) -> list[TrendItem]:
    items = []
    for video in data.get("items", []):
        snip, stats = video["snippet"], video.get("statistics", {})
        views = int(stats.get("viewCount", 0))
        items.append(TrendItem(
            source="youtube_trending",
            external_id=video["id"],
            title=snip["title"],
            url=f"https://www.youtube.com/watch?v={video['id']}",
            strength=log_strength(views, 10_000_000),
            published_at=_parse_iso(snip.get("publishedAt")),
            context=f"channel: {snip.get('channelTitle', '')}; tags: {', '.join(snip.get('tags', [])[:8])}",
            raw={"views": views, "category_id": snip.get("categoryId"), "channel": snip.get("channelTitle")},
        ))
    return items


@dataclass
class YouTubeTrending:
    api_key: str
    geo: str
    max_results: int = 50
    name: str = "youtube_trending"

    def fetch(self, client: httpx.Client) -> list[TrendItem]:
        resp = client.get("https://www.googleapis.com/youtube/v3/videos", params={
            "part": "snippet,statistics", "chart": "mostPopular", "regionCode": self.geo,
            "maxResults": self.max_results, "key": self.api_key,
        })
        resp.raise_for_status()
        return parse_youtube_popular(resp.json())


# --- Generic RSS / Atom feeds (subreddits, blogs, niche news) --------------------------------------


def parse_rss(xml: str, feed_url: str) -> list[TrendItem]:
    feed = feedparser.parse(xml)
    total = len(feed.entries)
    return [
        TrendItem(
            source="rss",
            external_id=entry.get("id") or entry.get("link") or entry.title,
            title=html.unescape(entry.title),
            url=entry.get("link"),
            strength=rank_strength(rank, total),
            published_at=_parse_date(entry.get("published") or entry.get("updated")),
            raw={"feed": feed_url},
        )
        for rank, entry in enumerate(feed.entries)
        if entry.get("title")
    ]


@dataclass
class RssFeeds:
    feeds: list[str]
    name: str = "rss"

    def fetch(self, client: httpx.Client) -> list[TrendItem]:
        items: list[TrendItem] = []
        for url in self.feeds:
            resp = client.get(url)
            resp.raise_for_status()
            items += parse_rss(resp.text, url)
        return items


# --- helpers / registry ---------------------------------------------------------------------------


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(UTC)
    except (TypeError, ValueError):
        return _parse_iso(value)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def build_sources(settings) -> list[TrendSource]:
    """Instantiate the sources enabled in settings.yaml (skipping ones missing credentials)."""
    cfg, ch = settings.trends.sources, settings.channel

    def on(name: str) -> bool:
        return bool(cfg.get(name, {}).get("enabled"))

    sources: list[TrendSource] = []
    if on("google_trends"):
        sources.append(GoogleTrends(geo=ch.geo))
    if on("google_news"):
        sources.append(GoogleNews(geo=ch.geo, language=ch.language))
    if on("wikipedia"):
        w = cfg["wikipedia"]
        sources.append(Wikipedia(project=w.get("project", "en.wikipedia"), top_n=w.get("top_n", 50)))
    if on("hackernews"):
        sources.append(HackerNews(min_points=cfg["hackernews"].get("min_points", 150)))
    if on("youtube_trending") and settings.env.youtube_api_key:
        sources.append(YouTubeTrending(api_key=settings.env.youtube_api_key, geo=ch.geo,
                                       max_results=cfg["youtube_trending"].get("max_results", 50)))
    if on("rss") and cfg["rss"].get("feeds"):
        sources.append(RssFeeds(feeds=cfg["rss"]["feeds"]))
    return sources
