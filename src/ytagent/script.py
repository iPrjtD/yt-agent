"""Short script writer: research brief -> structured script (hook, beats with visuals, outro, metadata)."""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from ytagent.config import ROOT, get_settings
from ytagent.providers import llm
from ytagent.research import Brief

log = logging.getLogger(__name__)

MIN_WORDS, MAX_WORDS = 100, 130  # Buzzbee's expressive voice is ~2.1 words/s; keeps Shorts under a minute


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


Emotion = Literal["excited", "serious", "dramatic", "warm", "playful", "calm", "shocked", "inspiring"]


class Shot(BaseModel):
    words: str = Field(description="the first 3 words of the phrase this shot covers, copied exactly")
    query: str = Field(description="2-5 word stock-footage search that literally shows those words")
    scene: str = Field(description="one vivid sentence describing the exact scene, for an illustration")


class BeatPlan(BaseModel):
    emotion: Emotion
    reaction: Literal["none", "shocked", "laugh", "cool", "think", "sad", "love"]
    sfx: Literal["none", "whoosh", "boom", "pop", "ding", "scratch", "boing"]
    meme_top: str
    meme_bottom: str
    buzz_line: str
    shots: list[Shot] = Field(min_length=1, max_length=4)


class MemePlan(BaseModel):
    hook: BeatPlan
    beats: list[BeatPlan]
    outro_emotion: Emotion


def plan_meme(script: ShortScript, fmt: str) -> MemePlan:
    """Second pass: emotions, Buzzbee reactions, meme captions, sound effects and quips for each beat."""
    numbered = "\n".join(f"{i}: {line}" for i, line in enumerate([script.hook, *(b.narration for b in script.beats)]))
    prompt = (ROOT / "prompts" / "meme_plan.md").read_text(encoding="utf-8").format(
        channel=get_settings().channel.name, format_name=fmt, lines=numbered)
    plan = llm.complete_json("writer", [{"role": "user", "content": prompt}], MemePlan, temperature=0.8)
    blanks = [BeatPlan(emotion="warm", reaction="none", sfx="none", meme_top="", meme_bottom="", buzz_line="",
                       shots=[Shot(words="", query=b.visual_query, scene=b.visual_query)]) for b in script.beats]
    plan.beats = (plan.beats + blanks[len(plan.beats):])[: len(script.beats)]
    return plan


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
