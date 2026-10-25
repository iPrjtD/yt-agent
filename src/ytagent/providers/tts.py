"""Buzzbee's voice: expressive single-voice narration with Gemini TTS (one request per Short).

Free-tier quota is per model per day, so several TTS models are tried in order. Models differ in what they
accept: some follow "[emotion]" tags and a direction silently, others read them aloud, so those get the plain
script only (see `voice.tts_plain_models`).
"""

from __future__ import annotations

import base64
import io
import logging
import re
import wave
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import numpy as np

from ytagent.config import get_settings
from ytagent.providers.llm import _exhausted, _mark_exhausted

log = logging.getLogger(__name__)

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


@dataclass
class Line:
    speaker: str   # kept for captions/animation ("Narrator" lines vs Buzzbee's quick asides); same voice
    text: str
    emotion: str = "warm"


def _decode(inline: dict) -> tuple[np.ndarray, int]:
    data = base64.b64decode(inline["data"])
    mime = inline.get("mimeType", "")
    if "wav" in mime:
        with wave.open(io.BytesIO(data)) as w:
            sr = w.getframerate()
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    else:  # raw 16-bit PCM, e.g. "audio/l16; rate=24000"
        m = re.search(r"rate=(\d+)", mime)
        sr = int(m.group(1)) if m else 24000
        pcm = np.frombuffer(data, dtype=np.int16)
    return pcm.astype(np.float32) / 32768.0, sr


def tagged_prompt(lines: list[Line], style: str) -> str:
    """Direction + one "[emotion] line" per row, for models that follow tags without speaking them."""
    direction = (f"You are Buzzbee, {style}. Voice this YouTube Short in one take. Perform each line with the "
                 "emotion in square brackets but never say the brackets or the emotion word. Natural, lively "
                 "pace with short pauses between lines. Say only the lines.")
    return direction + "\n\n" + "\n".join(f"[{ln.emotion}] {ln.text}" for ln in lines)


def plain_prompt(lines: list[Line]) -> str:
    """Just the words, for models that read any direction or tag aloud."""
    return "\n".join(ln.text for ln in lines)


def synthesize(lines: list[Line], voice: str, style: str, models: list[str],
               plain_models: set[str] | None = None) -> Iterator[tuple[np.ndarray, int, str]]:
    """Yields one take per available model, in order; the caller checks each and keeps the first good one."""
    key = get_settings().env.gemini_api_key
    if not key:
        return
    speech = {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}
    gen = {"responseModalities": ["AUDIO"], "speechConfig": speech}
    plain_models = plain_models or set()
    for model in models:
        if f"gemini/{model}" in _exhausted():
            continue
        text = plain_prompt(lines) if model in plain_models else tagged_prompt(lines, style)
        try:
            resp = httpx.post(API.format(model=model), headers={"x-goog-api-key": key}, timeout=180,
                              json={"contents": [{"parts": [{"text": text}]}], "generationConfig": gen})
        except httpx.HTTPError as exc:
            log.warning("tts %s failed: %s", model, exc)
            continue
        if resp.status_code == 429:
            _mark_exhausted(f"gemini/{model}", Exception(resp.text))
            continue
        if resp.status_code != 200:
            log.warning("tts %s HTTP %s: %s", model, resp.status_code, resp.text[:200])
            continue
        parts = resp.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
        inline = next((p["inlineData"] for p in parts if "inlineData" in p), None)
        if inline:
            samples, sr = _decode(inline)
            log.info("tts %s: %.1fs audio", model, len(samples) / sr)
            yield samples, sr, model
