"""Publishing: a publish package for manual upload, or a private API upload into "Pending review"."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from googleapiclient.http import MediaFileUpload

from ytagent.config import get_settings
from ytagent.research import Source
from ytagent.script import ShortScript
from ytagent.youtube import data_api

log = logging.getLogger(__name__)

# YouTube categories: 22 People & Blogs, 23 Comedy, 24 Entertainment, 27 Education
CATEGORY = {"facts": "27", "funny": "23", "motivation": "22", "story": "24", "trending": "24", "money": "27",
            "world": "19"}
CATEGORY_NAME = {"19": "Travel & Events", "22": "People & Blogs", "23": "Comedy", "24": "Entertainment",
                 "27": "Education"}
PLAYLIST = {"facts": "🧠 Bee Curious", "funny": "😂 Funny Buzz", "motivation": "🔥 Bee Inspired",
            "story": "🎬 The Real Story", "trending": "🐝 Buzz Check", "money": "💰 Money Buzz",
            "world": "🌍 World Buzz"}
HASHTAGS = "#shorts #buzzbee"


def full_description(script: ShortScript, sources: list[Source], credits: list[str] | None = None) -> str:
    body = re.split(r"\n\s*Sources?:", script.description)[0].strip()
    used = [sources[i - 1] for i in script.sources_used if 0 < i <= len(sources)] or sources
    lines = [body, "", "Sources:"] + [f"- {s.url}" for s in used] if used else [body]
    if credits:
        lines += ["", "Credits:"] + [f"- {c}" for c in dict.fromkeys(credits)]
    return "\n".join([*lines, "", HASHTAGS])[:4900]


def write_package(work: Path, script: ShortScript, description: str) -> Path:
    pkg = work / "publish.txt"
    synthetic = "Yes" if get_settings().publishing.contains_synthetic_media else "No"
    pkg.write_text(
        f"TITLE\n{script.title}\n\n"
        f"DESCRIPTION\n{description}\n\n"
        f"TAGS\n{', '.join(script.tags)}\n\n"
        f"SETTINGS\n- Category: {CATEGORY_NAME[CATEGORY[script.pillar]]}\n"
        f"- Audience: No, it's not made for kids\n"
        f"- Altered or synthetic content: {synthetic}\n"
        f"- Playlist: {PLAYLIST[script.pillar]}\n",
        encoding="utf-8")
    return pkg


def _playlist_id(yt, title: str) -> str | None:
    resp = yt.playlists().list(part="snippet", mine=True, maxResults=50).execute()
    return next((p["id"] for p in resp["items"] if p["snippet"]["title"] == title), None)


def upload_private(final: Path, script: ShortScript, description: str) -> str:
    """Upload as private and file it under "Pending review". Returns the YouTube video id."""
    yt = data_api()
    body = {
        "snippet": {"title": script.title[:100], "description": description, "tags": script.tags,
                    "categoryId": CATEGORY[script.pillar], "defaultLanguage": "en", "defaultAudioLanguage": "en"},
        "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
                   "containsSyntheticMedia": get_settings().publishing.contains_synthetic_media},
    }
    media = MediaFileUpload(str(final), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk()
    video_id = response["id"]
    playlist = _playlist_id(yt, "Pending review")
    if playlist:
        yt.playlistItems().insert(part="snippet", body={"snippet": {
            "playlistId": playlist, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}).execute()
    log.info("uploaded %s (private, pending review)", video_id)
    return video_id
