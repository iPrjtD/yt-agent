"""LLM access through LiteLLM, addressed by role (fast / writer / embed).

Moving from a local Ollama model to a hosted one is a settings.yaml change, e.g.
writer: anthropic/claude-sonnet-5-5 (plus ANTHROPIC_API_KEY in .env).
"""

from __future__ import annotations

import logging
import re
from typing import Literal

import litellm
from pydantic import BaseModel, ValidationError

from ytagent.config import get_settings

log = logging.getLogger(__name__)
litellm.suppress_debug_info = True

Role = Literal["fast", "writer"]

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _is_ollama(model: str) -> bool:
    return model.startswith(("ollama/", "ollama_chat/"))


def _model_kwargs(model: str) -> dict:
    settings = get_settings()
    kwargs: dict = {"timeout": settings.llm.timeout_s}
    if _is_ollama(model):
        kwargs["api_base"] = settings.env.ollama_api_base
    if model.startswith("gemini/"):
        kwargs["api_key"] = settings.env.gemini_api_key
    return kwargs


def _models(role: Role) -> list[str]:
    cfg = get_settings().llm
    models = [getattr(cfg, role)]
    if role == "writer" and cfg.writer_fallback and cfg.writer_fallback not in models:
        models.append(cfg.writer_fallback)
    return models


def complete(role: Role, messages: list[dict], *, response_format: dict | None = None, temperature: float = 0.7,
             think: bool = False) -> str:
    """`think` enables a local reasoning model's thinking phase: better answers, but minutes on a CPU.

    If the role's model fails (rate limit, outage, missing key), the configured fallback is tried.
    """
    models = _models(role)
    for i, model in enumerate(models):
        kwargs = _model_kwargs(model)
        if _is_ollama(model):
            kwargs["think"] = think
        if response_format:
            kwargs["response_format"] = response_format
        try:
            resp = litellm.completion(model=model, messages=messages, temperature=temperature, **kwargs)
        except Exception as exc:
            if i == len(models) - 1:
                raise
            log.warning("%s failed (%s); falling back to %s", model, type(exc).__name__, models[i + 1])
            continue
        text = resp.choices[0].message.content or ""
        return _THINK_RE.sub("", text).strip()
    raise RuntimeError("no model configured")


def complete_json[M: BaseModel](role: Role, messages: list[dict], schema: type[M], *, retries: int = 2,
                                temperature: float = 0.2) -> M:
    """Ask for JSON matching `schema`; on invalid output, show the error to the model and retry.

    The schema goes in response_format (constrained decoding on Ollama), not in the prompt: on a CPU
    every prompt token costs time.
    """
    response_format = {"type": "json_schema",
                       "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema(), "strict": True}}
    convo = list(messages)
    last_err: Exception | None = None
    for _ in range(retries + 1):
        text = complete(role, convo, response_format=response_format, temperature=temperature)
        try:
            return schema.model_validate_json(_extract_json(text))
        except (ValidationError, ValueError) as err:
            last_err = err
            log.warning("LLM JSON invalid, retrying: %s", err)
            convo += [
                {"role": "assistant", "content": text},
                {"role": "user", "content": f"That was invalid: {err}. Send the corrected JSON only."},
            ]
    raise ValueError(f"LLM did not return valid {schema.__name__}: {last_err}")


def gemini_models() -> list[str]:
    """Gemini models this key can use for text generation (newest names first)."""
    import httpx

    key = get_settings().env.gemini_api_key
    if not key:
        raise RuntimeError("GEMINI_API_KEY is empty in .env")
    resp = httpx.get("https://generativelanguage.googleapis.com/v1beta/models", params={"key": key, "pageSize": 200},
                     timeout=30)
    resp.raise_for_status()
    names = [m["name"].removeprefix("models/") for m in resp.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    return sorted(names, reverse=True)


def embed(texts: list[str]) -> list[list[float]]:
    model = get_settings().llm.embed
    resp = litellm.embedding(model=model, input=texts, **_model_kwargs(model))
    return [item["embedding"] for item in resp.data]


def _extract_json(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in reply")
    return text[start : end + 1]
