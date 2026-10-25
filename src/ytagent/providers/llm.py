"""LLM access through LiteLLM, addressed by role (fast / writer / embed).

Moving from a local Ollama model to a hosted one is a settings.yaml change, e.g.
writer: anthropic/claude-sonnet-5-5 (plus ANTHROPIC_API_KEY in .env).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Literal

# LiteLLM otherwise downloads its model price list from GitHub on import (times out on some networks, ~90 s).
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
import litellm  # noqa: E402
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
    kwargs: dict = {"timeout": settings.llm.timeout_s if _is_ollama(model) else settings.llm.hosted_timeout_s}
    if _is_ollama(model):
        kwargs["api_base"] = settings.env.ollama_api_base
    if model.startswith("gemini/"):
        kwargs["api_key"] = settings.env.gemini_api_key
    return kwargs


def _quota_file():
    return get_settings().data_dir / "llm_quota.json"


def _exhausted() -> dict[str, float]:
    """Models whose daily quota ran out, mapped to the epoch time they can be used again."""
    try:
        data = json.loads(_quota_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {m: t for m, t in data.items() if t > time.time()}


def _mark_exhausted(model: str, exc: Exception) -> None:
    """Remember a per-day quota hit (e.g. 'GenerateRequestsPerDay...', 'retryDelay': '69084s')."""
    text = str(exc)
    if "PerDay" not in text:
        return
    m = re.search(r'"retryDelay":\s*"(\d+)s"', text)
    until = time.time() + (int(m.group(1)) if m else 6 * 3600)
    data = {**_exhausted(), model: until}
    _quota_file().write_text(json.dumps(data), encoding="utf-8")
    log.warning("%s daily quota used up; skipping it for %.1f h", model, (until - time.time()) / 3600)


def _models(role: Role) -> list[str]:
    cfg = get_settings().llm
    models = list(dict.fromkeys([getattr(cfg, role), *cfg.fallbacks.get(role, [])]))
    skip = _exhausted()
    available = [m for m in models if m not in skip]
    return available or models[-1:]


_TRANSIENT = (litellm.ServiceUnavailableError, litellm.RateLimitError, litellm.InternalServerError,
              litellm.APIConnectionError, litellm.Timeout)


def _call_with_retry(model: str, messages: list[dict], kwargs: dict, attempts: int = 2):
    """Hosted models: retry short overloads / rate-limit bursts with back-off before giving up."""
    tries = 1 if _is_ollama(model) else attempts
    for attempt in range(tries):
        try:
            return litellm.completion(model=model, messages=messages, **kwargs)
        except _TRANSIENT as exc:
            if attempt == tries - 1:
                raise
            wait = 5 * (attempt + 1)
            log.info("%s busy (%s), retrying in %ds", model, type(exc).__name__, wait)
            time.sleep(wait)
    raise RuntimeError("unreachable")


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
        if not model.startswith("gemini/"):  # Gemini 3+ deprecates sampling params; it uses its own defaults
            kwargs["temperature"] = temperature
        try:
            resp = _call_with_retry(model, messages, kwargs)
        except Exception as exc:
            if isinstance(exc, litellm.RateLimitError):
                _mark_exhausted(model, exc)
            if i == len(models) - 1:
                raise
            reason = " ".join(str(exc).split())[:240]
            log.warning("%s failed (%s: %s); falling back to %s", model, type(exc).__name__, reason, models[i + 1])
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
