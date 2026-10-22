"""Voice-over (Kokoro, local) and word-level timing (faster-whisper aligned back to the script words)."""

from __future__ import annotations

import difflib
import logging
import re
import wave
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from ytagent.config import ROOT, get_settings

log = logging.getLogger(__name__)

MODELS = ROOT / "models"


@dataclass
class Word:
    text: str
    start: float
    end: float
    line: int


@dataclass
class Narration:
    wav: Path
    duration: float
    spans: list[tuple[float, float]]  # (start, end) of each spoken line
    words: list[Word]
    samples: np.ndarray | None = None  # mono float32, for lip-sync envelopes
    sr: int = 24000
    engine: str = "kokoro"


@lru_cache(maxsize=1)
def _kokoro():
    from kokoro_onnx import Kokoro

    return Kokoro(str(MODELS / "kokoro" / "kokoro-v1.0.onnx"), str(MODELS / "kokoro" / "voices-v1.0.bin"))


@lru_cache(maxsize=1)
def _whisper():
    from faster_whisper import WhisperModel

    name = get_settings().captions.whisper_model
    return WhisperModel(name, device="cpu", compute_type="int8", download_root=str(MODELS / "whisper"))


def _write_wav(path: Path, samples: np.ndarray, sr: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())


def _norm(token: str) -> str:
    return re.sub(r"[^\w%]", "", token.casefold())


def align_words(lines: list[str], spans: list[tuple[float, float]],
                heard: list[tuple[str, float, float]]) -> list[Word]:
    """Give every script word a time: copy whisper timings where words match, interpolate the rest."""
    script = [(tok, i) for i, line in enumerate(lines) for tok in line.split()]
    a = [_norm(t) for t, _ in script]
    b = [_norm(t) for t, _, _ in heard]
    times: list[tuple[float, float] | None] = [None] * len(script)
    for block in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_matching_blocks():
        for k in range(block.size):
            _, s, e = heard[block.b + k]
            times[block.a + k] = (s, e)
    words = []
    for line_idx, (line_start, line_end) in enumerate(spans):
        idx = [j for j, (_, li) in enumerate(script) if li == line_idx]
        for pos, j in enumerate(idx):
            if times[j] is None or not (line_start - 0.3 <= times[j][0] <= line_end + 0.3):
                # Spread unmatched words evenly over the line.
                step = (line_end - line_start) / max(1, len(idx))
                times[j] = (line_start + pos * step, line_start + (pos + 1) * step)
        for j in idx:
            words.append(Word(script[j][0], times[j][0], times[j][1], line_idx))
    # Keep start times monotonic (captions switch on each word's start).
    for prev, cur in zip(words, words[1:], strict=False):
        cur.start = max(cur.start, prev.start)
        cur.end = max(cur.end, cur.start)
    return words


def align_global(lines: list[str], heard: list[tuple[str, float, float]], duration: float) -> list[Word]:
    """Times for every script word when line boundaries are unknown (one-take narration).

    Words whisper heard keep their times; the rest are spread evenly between their known neighbours.
    """
    script = [(tok, i) for i, line in enumerate(lines) for tok in line.split()]
    a = [_norm(t) for t, _ in script]
    b = [_norm(t) for t, _, _ in heard]
    times: list[tuple[float, float] | None] = [None] * len(script)
    for block in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_matching_blocks():
        for k in range(block.size):
            _, s, e = heard[block.b + k]
            times[block.a + k] = (s, e)
    j = 0
    while j < len(script):
        if times[j] is not None:
            j += 1
            continue
        gap_end = j
        while gap_end < len(script) and times[gap_end] is None:
            gap_end += 1
        lo = times[j - 1][1] if j > 0 else 0.0
        hi = times[gap_end][0] if gap_end < len(script) else duration
        step = max(0.05, (hi - lo) / (gap_end - j))
        for n, idx in enumerate(range(j, gap_end)):
            times[idx] = (lo + n * step, lo + (n + 1) * step)
        j = gap_end
    words = [Word(tok, t[0], t[1], li) for (tok, li), t in zip(script, times, strict=True)]
    for prev, cur in zip(words, words[1:], strict=False):
        cur.start = max(cur.start, prev.start)
        cur.end = max(cur.end, cur.start)
    return words


def _transcribe(samples: np.ndarray, sr: int, prompt: str) -> list[tuple[str, float, float]]:
    n16 = int(len(samples) * 16000 / sr)
    audio16 = np.interp(np.linspace(0, len(samples), n16, endpoint=False), np.arange(len(samples)), samples)
    segments, _ = _whisper().transcribe(audio16.astype(np.float32), language="en", word_timestamps=True,
                                        initial_prompt=prompt[:800], vad_filter=False)
    return [(w.word.strip(), w.start, w.end) for seg in segments for w in (seg.words or [])]


def _trim_silence(samples: np.ndarray, sr: int, threshold: float = 0.01, pad: float = 0.08) -> np.ndarray:
    loud = np.flatnonzero(np.abs(samples) > threshold)
    if loud.size == 0:
        return samples
    a, b = max(0, loud[0] - int(pad * sr)), min(len(samples), loud[-1] + int(pad * sr))
    return samples[a:b]


def narrate_lines(lines: list, out_dir: Path, fmt) -> Narration:
    """Expressive one-take narration (Gemini TTS, narrator + Buzzbee) with Kokoro line-by-line fallback.

    `lines` are providers.tts.Line items; `fmt` is the format's FormatCfg (voices, style).
    """
    from ytagent.providers.tts import synthesize

    cfg = get_settings().voice
    texts = [ln.text for ln in lines]
    result = synthesize(lines, fmt.gemini_voice if fmt else "Puck", cfg.buzzbee_voice,
                        fmt.style if fmt else "a friendly, engaging host", cfg.gemini_tts_models)
    if result is None:
        log.info("Gemini TTS unavailable; using Kokoro")
        voices = [cfg.buzzbee_kokoro_voice if ln.speaker == "Buzzbee" else (fmt.voice if fmt else None)
                  for ln in lines]
        return narrate(texts, out_dir, voice=fmt.voice if fmt else None, speed=fmt.speed if fmt else None,
                       voices=voices)
    samples, sr, model = result
    samples = _trim_silence(samples, sr)
    wav = out_dir / "voice.wav"
    _write_wav(wav, samples, sr)
    duration = len(samples) / sr
    words = align_global(texts, _transcribe(samples, sr, " ".join(texts)), duration)
    spans = []
    for i in range(len(lines)):
        mine = [w for w in words if w.line == i]
        spans.append((mine[0].start, mine[-1].end) if mine else (spans[-1][1] if spans else 0.0,) * 2)
    log.info("narration (%s) %.1fs, %d words", model, duration, len(words))
    return Narration(wav=wav, duration=duration, spans=spans, words=words, samples=samples, sr=sr, engine=model)


def narrate(lines: list[str], out_dir: Path, voice: str | None = None, speed: float | None = None,
            voices: list[str | None] | None = None) -> Narration:
    cfg = get_settings().voice
    voice, speed = voice or cfg.voice, speed or cfg.speed
    kokoro = _kokoro()
    chunks, spans, t, sr = [], [], 0.0, 24000
    for idx, line in enumerate(lines):
        line_voice = (voices[idx] if voices and voices[idx] else None) or voice
        samples, sr = kokoro.create(line, voice=line_voice, speed=speed, lang="en-us")
        samples = np.trim_zeros(samples, "fb")
        dur = len(samples) / sr
        spans.append((t, t + dur))
        chunks += [samples, np.zeros(int(cfg.line_gap_s * sr), dtype=samples.dtype)]
        t += dur + cfg.line_gap_s
    audio = np.concatenate(chunks)
    wav = out_dir / "voice.wav"
    _write_wav(wav, audio, sr)

    # Pass samples (16 kHz float32) instead of the file: faster-whisper's file decoding breaks on newer PyAV.
    heard = _transcribe(audio, sr, " ".join(lines))
    words = align_words(lines, spans, heard)
    log.info("narration %.1fs, %d words (%d heard)", len(audio) / sr, len(words), len(heard))
    return Narration(wav=wav, duration=len(audio) / sr, spans=spans, words=words, samples=audio, sr=sr)
