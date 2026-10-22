"""Original sound effects synthesised in code (no licences needed): whoosh, boom, pop, ding, scratch, boing.

Drop your own file at assets/sfx/<name>.wav (or .mp3) to override any of them.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from ytagent.config import ROOT

SR = 48000
NAMES = ("whoosh", "boom", "pop", "ding", "scratch", "boing")
USER_DIR = ROOT / "assets" / "sfx"


def _env(n: int, attack: float, decay: float) -> np.ndarray:
    t = np.arange(n) / SR
    return np.minimum(1.0, t / max(attack, 1e-4)) * np.exp(-t / decay)


def _noise(n: int, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(n)


def _lowpass(x: np.ndarray, alpha: np.ndarray | float) -> np.ndarray:
    y = np.empty_like(x)
    acc = 0.0
    a = np.broadcast_to(alpha, x.shape)
    for i, v in enumerate(x):
        acc += a[i] * (v - acc)
        y[i] = acc
    return y


def synth(name: str) -> np.ndarray:
    if name == "whoosh":  # band of noise sweeping up then down
        n = int(0.55 * SR)
        sweep = 0.02 + 0.25 * np.sin(np.linspace(0, np.pi, n)) ** 2
        x = _lowpass(_noise(n, 1), sweep) * np.sin(np.linspace(0, np.pi, n)) ** 1.5
    elif name == "boom":  # deep pitch-dropping thump with a noise click
        n = int(1.2 * SR)
        t = np.arange(n) / SR
        freq = 38 + 90 * np.exp(-t / 0.08)
        x = np.sin(2 * np.pi * np.cumsum(freq) / SR) * _env(n, 0.002, 0.35)
        x[: int(0.02 * SR)] += 0.4 * _noise(int(0.02 * SR), 2)
    elif name == "pop":  # short bubbly blip
        n = int(0.12 * SR)
        t = np.arange(n) / SR
        x = np.sin(2 * np.pi * (500 + 1400 * t / 0.12) * t) * _env(n, 0.001, 0.03)
    elif name == "ding":  # bright bell
        n = int(1.0 * SR)
        t = np.arange(n) / SR
        x = sum(a * np.sin(2 * np.pi * f * t) for f, a in ((1760, 1.0), (2640, 0.5), (4400, 0.25)))
        x = x * _env(n, 0.001, 0.25)
    elif name == "scratch":  # record scratch: wobbling filtered noise
        n = int(0.5 * SR)
        t = np.arange(n) / SR
        wobble = 0.15 + 0.12 * np.sin(2 * np.pi * 9 * t)
        x = _lowpass(_noise(n, 3), wobble) * (1 - t / 0.5) * (np.sin(2 * np.pi * 9 * t) > -0.3)
    elif name == "boing":  # cartoon spring
        n = int(0.6 * SR)
        t = np.arange(n) / SR
        freq = 180 + 260 * np.exp(-t / 0.25) * (1 + 0.35 * np.sin(2 * np.pi * 14 * t))
        x = np.sin(2 * np.pi * np.cumsum(freq) / SR) * _env(n, 0.003, 0.25)
    else:
        raise ValueError(f"unknown sfx {name!r}")
    return (0.9 * x / (np.max(np.abs(x)) or 1)).astype(np.float32)


def sfx_file(name: str, cache_dir: Path) -> Path:
    """A user-provided effect if present, else the synthesised one (cached as WAV)."""
    for ext in (".wav", ".mp3", ".ogg"):
        own = USER_DIR / f"{name}{ext}"
        if own.exists():
            return own
    path = cache_dir / f"sfx_{name}.wav"
    if not path.exists():
        cache_dir.mkdir(parents=True, exist_ok=True)
        data = (synth(name) * 32767).astype(np.int16)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(data.tobytes())
    return path
