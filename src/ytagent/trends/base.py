"""Shared types and helpers for trend sources."""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import httpx

USER_AGENT = "yt-agent/0.1 (trend research; https://github.com/iPrjtD)"


@dataclass
class TrendItem:
    source: str
    external_id: str
    title: str
    url: str | None = None
    # 0..1, normalised within the source so sources can be compared.
    strength: float = 0.0
    # How many related articles/items the source grouped with this one (coverage breadth).
    related: int = 0
    published_at: datetime | None = None
    # Extra text (news headlines, snippets) that helps the LLM understand the topic.
    context: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class TrendSource(Protocol):
    name: str

    def fetch(self, client: httpx.Client) -> list[TrendItem]: ...


_STOP = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "at", "by", "with", "from", "is", "are",
    "was", "were", "be", "as", "it", "its", "this", "that", "after", "over", "vs", "new", "how", "why",
    "what", "who", "will", "says", "said",
}


def topic_key(title: str, max_tokens: int = 6) -> str:
    """Order-insensitive key so "Messi joins Inter" and "Inter: Messi joins" collide."""
    text = unicodedata.normalize("NFKC", title).casefold()
    tokens = [t for t in re.findall(r"\w+", text) if t not in _STOP and len(t) > 1]
    return " ".join(sorted(dict.fromkeys(tokens[:max_tokens])))[:200]


def log_strength(value: float, full_at: float) -> float:
    """Map a popularity count onto 0..1 on a log scale, reaching 1 at `full_at`."""
    if value <= 0:
        return 0.0
    return min(1.0, math.log10(value + 1) / math.log10(full_at + 1))


def rank_strength(rank: int, total: int) -> float:
    """1.0 for the first item of a ranked list, falling linearly to 0.2 for the last."""
    if total <= 1:
        return 1.0
    return round(1.0 - 0.8 * rank / (total - 1), 4)
