"""YouTube auth and API clients (Data API v3 + Analytics API v2) for the owner's channel."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from ytagent.config import ROOT, get_settings

log = logging.getLogger(__name__)

# "youtube" covers uploads, metadata edits, playlists and scheduling; analytics is read-only.
SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
TOKEN_PATH = ROOT / "secrets" / "youtube_token.json"


class NotLoggedIn(RuntimeError):
    pass


def _save(creds: Credentials) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")


def login() -> Credentials:
    """Interactive Google sign-in in the browser; pick the channel the agent should manage."""
    secrets = Path(ROOT / get_settings().env.youtube_client_secrets)
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline",
                                  authorization_prompt_message="Opening Google sign-in: {url}")
    _save(creds)
    return creds


def credentials() -> Credentials:
    if not TOKEN_PATH.exists():
        raise NotLoggedIn("no YouTube token: run `ytagent youtube login`")
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise NotLoggedIn(f"YouTube login expired or revoked ({exc}): run `ytagent youtube login`") from exc
        _save(creds)
        return creds
    raise NotLoggedIn("YouTube token invalid: run `ytagent youtube login`")


def data_api():
    return build("youtube", "v3", credentials=credentials(), cache_discovery=False)


def analytics_api():
    return build("youtubeAnalytics", "v2", credentials=credentials(), cache_discovery=False)


def my_channel() -> dict[str, Any]:
    resp = data_api().channels().list(part="snippet,statistics,status,brandingSettings", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        raise RuntimeError("the signed-in account has no YouTube channel")
    return items[0]
