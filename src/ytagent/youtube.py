"""YouTube auth and API clients (Data API v3 + Analytics API v2) for the owner's channel."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import httpx
from google.auth import exceptions as auth_exceptions
from google.auth import transport
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from ytagent.config import ROOT, get_settings

log = logging.getLogger(__name__)

# "youtube" covers uploads, metadata edits, playlists and scheduling; "force-ssl" is needed to post comments;
# analytics is read-only. Saved tokens keep the scopes they were granted with (an older token without
# force-ssl keeps working for everything except comments until the next `ytagent youtube login`).
SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
TOKEN_PATH = ROOT / "secrets" / "youtube_token.json"


class NotLoggedIn(RuntimeError):
    pass


class _Response(transport.Response):
    def __init__(self, resp: httpx.Response):
        self._resp = resp

    @property
    def status(self) -> int:
        return self._resp.status_code

    @property
    def headers(self):
        return self._resp.headers

    @property
    def data(self) -> bytes:
        return self._resp.content


class HttpxRequest(transport.Request):
    """Token refresh over httpx. On this PC `requests`/urllib3 TLS handshakes to Google intermittently fail
    (SSL EOF for an hour at a time) while httpx and curl keep working; the hourly refresh hit those windows."""

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        last = None
        for attempt in range(3):
            try:
                return _Response(httpx.request(method, url, content=body, headers=headers, timeout=timeout or 30))
            except httpx.HTTPError as exc:
                last = exc
                time.sleep(2 * (attempt + 1))
        raise auth_exceptions.TransportError(last) from last


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
    # No scopes argument: refreshing must ask only for what the saved token was granted.
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(HttpxRequest())
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
