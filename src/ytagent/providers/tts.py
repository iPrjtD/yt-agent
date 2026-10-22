"""Expressive narration with Gemini TTS: one request voices the whole Short, narrator + Buzzbee.

Free-tier quota is per model per day, so several TTS models are tried in order; on failure the caller falls
back to local Kokoro.
"""

from __future__ import annotations

import base64
import io
import logging
import re
import wave
from dataclasses import dataclass

import httpx
import numpy as np

from ytagent.config import get_settings
from ytagent.providers.llm import _exhausted, _mark_exhausted

log = logging.getLogger(__name__)

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


@dataclass
class Line:
    speaker: str   # "Narrator" or "Buzzbee"
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


def build_prompt(lines: list[Line], narrator_style: str) -> str:
    direction = (f"Voice this YouTube Shorts script. The Narrator is {narrator_style}. Buzzbee is a cheeky, "
                 "playful cartoon bee who makes quick funny comments. Perform each line with the emotion in "
                 "square brackets, but never say the brackets or the emotion word. Keep a natural, fast, "
                 "engaging pace with short pauses between lines.")
    body = "\n".join(f"{ln.speaker}: [{ln.emotion}] {ln.text}" for ln in lines)
    return f"{direction}\n\n{body}"


def synthesize(lines: list[Line], narrator_voice: str, buzz_voice: str, narrator_style: str,
               models: list[str]) -> tuple[np.ndarray, int, str] | None:
    """Returns (samples float32 mono, sample rate, model) or None if every model failed."""
    key = get_settings().env.gemini_api_key
    if not key:
        return None
    speakers = {ln.speaker for ln in lines}
    if len(speakers) > 1:
        speech = {"multiSpeakerVoiceConfig": {"speakerVoiceConfigs": [
            {"speaker": "Narrator", "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": narrator_voice}}},
            {"speaker": "Buzzbee", "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": buzz_voice}}}]}}
    else:
        speech = {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": narrator_voice}}}
    gen = {"responseModalities": ["AUDIO"], "speechConfig": speech}
    # Format A: one text with "Speaker: [emotion] line" rows + a direction (models that follow tags silently).
    prompt_payload = {"contents": [{"parts": [{"text": build_prompt(lines, narrator_style)}]}], "generationConfig": gen}
    # Format B: one part per line tagged with speechMetadata (newer models; they read [tags] aloud, so none).
    parts_payload = {"contents": [{"role": "user", "parts": [
        {"text": ln.text, "speechMetadata": {"speaker": ln.speaker}} for ln in lines]}], "generationConfig": gen}
    skip = _exhausted()
    for model in models:
        if f"gemini/{model}" in skip:
            continue
        try:
            resp = httpx.post(API.format(model=model), headers={"x-goog-api-key": key}, json=prompt_payload,
                              timeout=180)
            if resp.status_code == 400 and "speech_metadata" in resp.text and len(speakers) > 1:
                resp = httpx.post(API.format(model=model), headers={"x-goog-api-key": key}, json=parts_payload,
                                  timeout=180)
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
            return samples, sr, model
    return None
