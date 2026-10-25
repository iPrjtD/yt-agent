"""AI scene illustrations (free tier): Cloudflare Workers AI running FLUX.1 [schnell] (Apache-2.0 model).

Used only when no real footage matches a shot. Needs CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN
(token permission: Workers AI - Read/Run). A daily cap keeps usage inside the free allocation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ytagent.config import get_settings

log = logging.getLogger(__name__)

MODEL = "@cf/black-forest-labs/flux-1-schnell"


def available() -> bool:
    env = get_settings().env
    return bool(env.cloudflare_account_id and env.cloudflare_api_token) and _used_today() < _cap()


def _cap() -> int:
    return get_settings().visuals.ai_daily_cap


def _counter() -> Path:
    return get_settings().data_dir / "ai_images.json"


def _used_today() -> int:
    try:
        data = json.loads(_counter().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return data.get("count", 0) if data.get("day") == datetime.now(UTC).date().isoformat() else 0


def _count_one() -> None:
    day = datetime.now(UTC).date().isoformat()  # Cloudflare's free allocation resets at 00:00 UTC
    _counter().write_text(json.dumps({"day": day, "count": _used_today() + 1}), encoding="utf-8")


def generate(scene: str, cache_dir: Path) -> Path | None:
    """Draw `scene` in the channel's illustration style; cached by prompt. None if unavailable or failed."""
    cfg = get_settings()
    prompt = f"{scene.strip().rstrip('.')}. {cfg.visuals.ai_style}"
    path = cache_dir / f"ai_{hashlib.sha1(prompt.encode()).hexdigest()[:16]}.jpg"
    if path.exists():
        return path
    if not available():
        return None
    url = f"https://api.cloudflare.com/client/v4/accounts/{cfg.env.cloudflare_account_id}/ai/run/{MODEL}"
    try:
        resp = httpx.post(url, headers={"Authorization": f"Bearer {cfg.env.cloudflare_api_token}"},
                          json={"prompt": prompt[:2000], "steps": 4}, timeout=120)
    except httpx.HTTPError as exc:
        log.warning("AI image failed: %s", exc)
        return None
    if resp.status_code != 200:
        log.warning("AI image HTTP %s: %s", resp.status_code, resp.text[:200])
        return None
    image = (resp.json().get("result") or {}).get("image")
    if not image:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(image))
    _count_one()
    log.info("AI scene drawn (%d today): %s", _used_today(), scene[:80])
    return path
