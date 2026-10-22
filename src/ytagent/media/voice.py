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


def narrate(lines: list[str], out_dir: Path) -> Narration:
    cfg = get_settings().voice
    kokoro = _kokoro()
    chunks, spans, t, sr = [], [], 0.0, 24000
    for line in lines:
        samples, sr = kokoro.create(line, voice=cfg.voice, speed=cfg.speed, lang="en-us")
        samples = np.trim_zeros(samples, "fb")
        dur = len(samples) / sr
        spans.append((t, t + dur))
        chunks += [samples, np.zeros(int(cfg.line_gap_s * sr), dtype=samples.dtype)]
        t += dur + cfg.line_gap_s
    audio = np.concatenate(chunks)
    wav = out_dir / "voice.wav"
    _write_wav(wav, audio, sr)

    segments, _ = _whisper().transcribe(str(wav), language="en", word_timestamps=True,
                                        initial_prompt=" ".join(lines)[:800], vad_filter=False)
    heard = [(w.word.strip(), w.start, w.end) for seg in segments for w in (seg.words or [])]
    words = align_words(lines, spans, heard)
    log.info("narration %.1fs, %d words (%d heard)", len(audio) / sr, len(words), len(heard))
    return Narration(wav=wav, duration=len(audio) / sr, spans=spans, words=words)
