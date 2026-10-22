"""Local image-text matching with CLIP (ViT-B/32, quantized ONNX): does this footage show what we say?"""

from __future__ import annotations

from functools import lru_cache

import numpy as np
from PIL import Image

from ytagent.config import ROOT

MODEL_DIR = ROOT / "models" / "clip"
_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)


@lru_cache(maxsize=1)
def _sessions():
    import onnxruntime as ort
    from tokenizers import Tokenizer

    opts = ort.SessionOptions()
    opts.log_severity_level = 3
    vision = ort.InferenceSession(str(MODEL_DIR / "vision_model_quantized.onnx"), opts,
                                  providers=["CPUExecutionProvider"])
    text = ort.InferenceSession(str(MODEL_DIR / "text_model_quantized.onnx"), opts, providers=["CPUExecutionProvider"])
    tokenizer = Tokenizer.from_file(str(MODEL_DIR / "tokenizer.json"))
    tokenizer.enable_truncation(77)
    return vision, text, tokenizer


def _normalize(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


def _preprocess(img: Image.Image) -> np.ndarray:
    """CLIP preprocessing: shortest side 224 (bicubic), centre crop 224, scale, normalise, CHW."""
    img = img.convert("RGB")
    w, h = img.size
    scale = 224 / min(w, h)
    img = img.resize((max(224, round(w * scale)), max(224, round(h * scale))), Image.BICUBIC)
    w, h = img.size
    left, top = (w - 224) // 2, (h - 224) // 2
    arr = np.asarray(img.crop((left, top, left + 224, top + 224)), dtype=np.float32) / 255.0
    return ((arr - _MEAN) / _STD).transpose(2, 0, 1)


def embed_text(text: str) -> np.ndarray:
    _, session, tokenizer = _sessions()
    ids = np.array([tokenizer.encode(text).ids], dtype=np.int64)
    return _normalize(session.run(None, {"input_ids": ids})[0])[0]


def embed_images(images: list[Image.Image]) -> np.ndarray:
    session, _, _ = _sessions()
    batch = np.stack([_preprocess(i) for i in images])
    return _normalize(session.run(None, {"pixel_values": batch})[0])


def similarity(text: str, images: list[Image.Image]) -> list[float]:
    """Cosine similarity of each image to the text (~0.30 = clear match, <0.22 = usually unrelated)."""
    if not images:
        return []
    return (embed_images(images) @ embed_text(text)).tolist()


REAL_PROMPT = "a real photograph or real video footage"
FAKE_PROMPT = "a cartoon, illustration, clipart, vector graphic or 3D render"


def score_with_realism(text: str, images: list[Image.Image]) -> tuple[list[float], list[bool]]:
    """Similarity to `text`, plus whether each image looks like real footage rather than a cartoon/render."""
    if not images:
        return [], []
    emb = embed_images(images)
    sims = (emb @ embed_text(text)).tolist()
    real = emb @ embed_text(REAL_PROMPT)
    fake = emb @ embed_text(FAKE_PROMPT)
    return sims, [bool(r >= f) for r, f in zip(real, fake, strict=True)]
