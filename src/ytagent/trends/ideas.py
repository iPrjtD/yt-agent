"""Evergreen idea sources: Wikipedia "Did you know", LLM-proposed ideas, motivation from great people.

They produce TrendItems like the trend sources, so ideas flow through the same merge + scoring pipeline.
"""

from __future__ import annotations

import html
import random
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from ytagent.providers import llm
from ytagent.trends.base import TrendItem, topic_key

MOTIVATION_THEMES = ["failure", "hard work", "dreams", "fear", "learning", "patience", "success", "discipline",
                     "self-belief", "time"]


def parse_dyk(page_html: str) -> list[TrendItem]:
    items = []
    for li in re.findall(r"<li>(.*?)</li>", page_html, re.S):
        text = html.unescape(re.sub(r"<[^>]+>", "", li)).strip()
        if not text.startswith("... that"):
            continue
        fact = re.sub(r"\s*\((pictured|illustrated)\)", "", text[len("... that "):]).rstrip("?").strip()
        link = re.search(r'<b><a href="/wiki/([^"#]+)"', li) or re.search(r'<a href="/wiki/([^"#:]+)"', li)
        url = f"https://en.wikipedia.org/wiki/{link.group(1)}" if link else None
        items.append(TrendItem(source="wikipedia_dyk", external_id=topic_key(fact), title=f"Did you know: {fact}",
                               url=url, strength=0.55, context="format hint: facts or funny",
                               raw={"fact": fact}))
    return items


@dataclass
class WikipediaDYK:
    name: str = "wikipedia_dyk"

    def fetch(self, client: httpx.Client) -> list[TrendItem]:
        resp = client.get("https://en.wikipedia.org/w/api.php", params={
            "action": "parse", "page": "Template:Did_you_know", "prop": "text",
            "format": "json", "formatversion": 2})
        resp.raise_for_status()
        return parse_dyk(resp.json()["parse"]["text"])


class Idea(BaseModel):
    title: str = Field(description="the specific surprising fact, question or story, max 12 words")
    format: Literal["facts", "funny", "story"]


class IdeaList(BaseModel):
    ideas: list[Idea]


@dataclass
class EvergreenIdeas:
    categories: list[str]
    per_run: int = 8
    name: str = "evergreen"

    def fetch(self, client: httpx.Client) -> list[TrendItem]:
        rng = random.Random(datetime.now(UTC).strftime("%Y%m%d%H"))
        picked = rng.sample(self.categories, k=min(3, len(self.categories)))
        prompt = (
            f"Suggest {self.per_run} ideas for viral YouTube Shorts in these categories: {', '.join(picked)}.\n"
            "Each idea must be ONE specific, surprising and TRUE thing that can be verified on Wikipedia or a "
            "reputable site (e.g. 'Octopuses have three hearts and blue blood', 'The man who survived two "
            "atomic bombs'). Mix formats: 'facts' (mind-blowing facts), 'funny' (funny or weird facts), "
            "'story' (a short true story). Avoid politics, tragedies, medical advice and anything you are unsure "
            "is true. No generic topics like 'facts about space'.")
        ideas = llm.complete_json("fast", [{"role": "user", "content": prompt}], IdeaList).ideas
        return [TrendItem(source="evergreen", external_id=topic_key(i.title), title=i.title, strength=0.6,
                          context=f"format hint: {i.format}", raw={"format": i.format})
                for i in ideas[: self.per_run]]


@dataclass
class Motivation:
    people: list[str] = field(default_factory=list)
    per_run: int = 2
    name: str = "motivation"

    def fetch(self, client: httpx.Client) -> list[TrendItem]:
        rng = random.Random(datetime.now(UTC).strftime("%Y%m%d"))
        items = []
        for person in rng.sample(self.people, k=min(self.per_run, len(self.people))):
            theme = rng.choice(MOTIVATION_THEMES)
            title = f"{person} on {theme}"
            items.append(TrendItem(source="motivation", external_id=topic_key(title), title=title, strength=0.65,
                                   context=f"format hint: motivation; person: {person}; theme: {theme}",
                                   raw={"person": person, "theme": theme}))
        return items
