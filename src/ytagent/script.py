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
    subject: str = Field(description="the specific real person, film, event or place this beat is about, for "
                                     "archive footage (e.g. 'Charlie Chaplin The Tramp'); empty if generic")


class ShortScript(BaseModel):
    title: str = Field(max_length=100)
    hook: str
    hook_visual_query: str
    beats: list[Beat] = Field(min_length=3, max_length=7)
    outro: str
    description: str
    tags: list[str]
    pillar: Literal["facts", "funny", "motivation", "story", "trending"]
    sources_used: list[int] = Field(default_factory=list)

    def lines(self) -> list[str]:
        """Spoken lines in order: hook, each beat, outro."""
        return [self.hook, *(b.narration for b in self.beats), self.outro]

    def word_count(self) -> int:
        return sum(len(line.split()) for line in self.lines())


def write_short(brief: Brief, fmt: str = "facts", angle: str = "") -> ShortScript:
    ch = get_settings().channel
    guide = (ROOT / "prompts" / "formats" / f"{fmt}.md").read_text(encoding="utf-8").strip()
    prompt = (ROOT / "prompts" / "short_script.md").read_text(encoding="utf-8").format(
        channel=ch.name, topic=brief.topic, angle=angle or "whatever makes it most engaging",
        research=brief.as_prompt() or "(no sources found: only use widely known, non-controversial facts)",
        format_name=fmt, format_guide=guide, min_words=MIN_WORDS, max_words=MAX_WORDS,
    )
    messages = [{"role": "user", "content": prompt}]
    script = llm.complete_json("writer", messages, ShortScript, temperature=0.8)
    words = script.word_count()
    if not MIN_WORDS * 0.8 <= words <= MAX_WORDS * 1.2:
        log.info("script has %d words, asking for a resize", words)
        fix = (f"The script has {words} spoken words; it must have {MIN_WORDS}-{MAX_WORDS}. "
               "Rewrite it to fit, same JSON format.")
        messages += [{"role": "assistant", "content": script.model_dump_json()}, {"role": "user", "content": fix}]
        script = llm.complete_json("writer", messages, ShortScript)
    script = fact_check(script, brief, messages)
    script.pillar = fmt
    return script


class Issue(BaseModel):
    line: int = Field(description="index of the spoken line (0 = hook)")
    problem: str


class FactCheck(BaseModel):
    issues: list[Issue]


def fact_check(script: ShortScript, brief: Brief, messages: list[dict]) -> ShortScript:
    """Flag spoken lines not supported by the research; have the writer fix them once."""
    numbered = "\n".join(f"{i}: {line}" for i, line in enumerate(script.lines()))
    prompt = (f"Research:\n{brief.as_prompt()}\n\nScript lines:\n{numbered}\n\n"
              "List every line that states a fact, number, date, name or quote that the research does NOT "
              "support, or that exaggerates it. Opinions, questions, jokes and the follow invitation are fine. "
              "Quotes must match the research word for word. Return an empty list if everything is supported.")
    issues = llm.complete_json("writer", [{"role": "user", "content": prompt}], FactCheck).issues
    if not issues:
        return script
    log.info("fact check: %d issue(s): %s", len(issues), "; ".join(f"{i.line}: {i.problem}" for i in issues))
    fix = ("A fact check found problems:\n" + "\n".join(f"- line {i.line}: {i.problem}" for i in issues)
           + "\nRewrite the script so every claim is supported by the research (keep length and format), "
             "same JSON format.")
    messages = [*messages, {"role": "assistant", "content": script.model_dump_json()},
                {"role": "user", "content": fix}]
    return llm.complete_json("writer", messages, ShortScript)
