"""Animated Buzzbee host: pop-in reactions and talking, rendered as PNG frame sequences for FFmpeg overlay."""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

from ytagent.brand import mascot

SIZE = 380          # on-screen mascot size (px, 1080-wide frame)
CANVAS = 480        # frame size, leaves room for overshoot and wobble
POP_IN, POP_OUT = 0.28, 0.2


@dataclass
class Reaction:
    start: float                 # seconds in the final video
    duration: float
    expression: str              # brand.EXPRESSIONS, or "talk" for a speaking line
    frames_dir: Path | None = None


@lru_cache(maxsize=64)
def _pose(expression: str, mouth_level: int) -> Image.Image:
    """Cached mascot drawing; mouth levels 0-3 for talking."""
    return mascot(SIZE, expression if expression != "talk" else "neutral", mouth=mouth_level / 3)


def _scale_at(t: float, duration: float) -> float:
    if t < POP_IN:  # pop in with overshoot: 0 -> 1.15 -> 1.0
        x = t / POP_IN
        return 1.15 * math.sin(x * math.pi / 2) if x < 0.75 else 1.15 - 0.15 * (x - 0.75) / 0.25
    if t > duration - POP_OUT:
        return max(0.0, (duration - t) / POP_OUT)
    return 1.0


def mouth_levels(envelope: np.ndarray, fps: int) -> list[int]:
    """Map an audio loudness envelope (one value per frame, 0..1) to mouth levels 0-3."""
    if envelope.size == 0:
        return []
    peak = float(np.percentile(envelope, 95)) or 1.0
    return [min(3, int(4 * v / peak)) for v in envelope[: int(len(envelope))]]


def render_frames(reaction: Reaction, out_dir: Path, fps: int, envelope: np.ndarray | None = None) -> Path:
    """Write frame PNGs for one reaction and return the directory."""
    out_dir.mkdir(parents=True, exist_ok=True)
    n = max(1, round(reaction.duration * fps))
    levels = mouth_levels(envelope, fps) if envelope is not None else []
    for i in range(n):
        t = i / fps
        level = levels[i] if i < len(levels) else 0
        pose = _pose(reaction.expression, level if reaction.expression == "talk" else 0)
        scale = _scale_at(t, reaction.duration)
        frame = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
        if scale > 0.02:
            angle = 0.0
            if reaction.expression in ("laugh", "shocked"):
                angle = 6 * math.sin(t * 2 * math.pi * 4) * (1 if reaction.expression == "laugh" else 0.5)
            bob = 8 * math.sin(t * 2 * math.pi * 1.6)
            size = max(2, int(SIZE * scale))
            sprite = pose.resize((size, size), Image.BICUBIC).rotate(angle, resample=Image.BICUBIC, expand=False)
            frame.alpha_composite(sprite, ((CANVAS - size) // 2, int((CANVAS - size) / 2 + bob)))
        frame.save(out_dir / f"{i:04d}.png", compress_level=1)
    reaction.frames_dir = out_dir
    return out_dir


def loudness_envelope(samples: np.ndarray, sr: int, start: float, duration: float, fps: int) -> np.ndarray:
    """RMS loudness per video frame for a slice of the narration (drives the talking mouth)."""
    a, b = int(start * sr), int((start + duration) * sr)
    chunk = samples[a:b]
    hop = max(1, sr // fps)
    frames = [float(np.sqrt(np.mean(chunk[i:i + hop] ** 2))) if i < len(chunk) else 0.0
              for i in range(0, hop * max(1, round(duration * fps)), hop)]
    return np.array(frames, dtype=np.float32)
