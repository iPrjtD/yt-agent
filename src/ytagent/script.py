"""Short script writer: research brief -> structured script (hook, beats with visuals, outro, metadata)."""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from ytagent.config import ROOT, get_settings
from ytagent.providers import llm
from ytagent.research import Brief

log = logging.getLogger(__name__)

MIN_WORDS, MAX_WORDS = 110, 150


class Beat(BaseModel):
    narration: str
    visual_query: str
    on_screen: str


class ShortScript(BaseModel):
    title: str = Field(max_length=100)
    hook: str
    beats: list[Beat] = Field(min_length=3, max_length=7)
    outro: str
    description: str
    tags: list[str]
    pillar: Literal["trending", "curious", "story"]
    sources_used: list[int] = Field(default_factory=list)

    def lines(self) -> list[str]:
        """Spoken lines in order: hook, each beat, outro."""
        return [self.hook, *(b.narration for b in self.beats), self.outro]

    def word_count(self) -> int:
        return sum(len(line.split()) for line in self.lines())


def write_short(brief: Brief, angle: str = "") -> ShortScript:
    ch = get_settings().channel
    prompt = (ROOT / "prompts" / "short_script.md").read_text(encoding="utf-8").format(
        channel=ch.name, tagline=ch.tagline, topic=brief.topic, angle=angle or "explain why people care",
        research=brief.as_prompt() or "(no sources found: only use widely known, non-controversial facts)",
        min_words=MIN_WORDS, max_words=MAX_WORDS,
    )
    messages = [{"role": "user", "content": prompt}]
    script = llm.complete_json("writer", messages, ShortScript)
    words = script.word_count()
    if not MIN_WORDS * 0.8 <= words <= MAX_WORDS * 1.2:
        log.info("script has %d words, asking for a resize", words)
        fix = (f"The script has {words} spoken words; it must have {MIN_WORDS}-{MAX_WORDS}. "
               "Rewrite it to fit, same JSON format.")
        messages += [{"role": "assistant", "content": script.model_dump_json()}, {"role": "user", "content": fix}]
        script = llm.complete_json("writer", messages, ShortScript)
    return script
