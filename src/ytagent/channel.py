"""One-off channel setup: retire old videos, apply branding, create playlists.

Every step is idempotent and supports dry runs. Old video ids are logged to data/ so the change can be undone.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from google.auth.transport.requests import AuthorizedSession
from googleapiclient.http import MediaFileUpload

from ytagent.brand import OUT as BRAND_DIR
from ytagent.config import get_settings
from ytagent.youtube import credentials, data_api

log = logging.getLogger(__name__)

# Writable fields of a video's `status` part; videos.update resets any writable field that is omitted.
_WRITABLE_STATUS = ("privacyStatus", "embeddable", "license", "publicStatsViewable", "publishAt",
                    "selfDeclaredMadeForKids", "containsSyntheticMedia")


@dataclass
class PlaylistSpec:
    title: str
    description: str
    privacy: str = "public"


PLAYLISTS = [
    PlaylistSpec("🐝 Buzz Check", "Everything people are talking about right now, in under a minute."),
    PlaylistSpec("🧠 Bee Curious", "Mind-blowing facts about science, space, history and the world around us."),
    PlaylistSpec("🎬 The Real Story", "Incredible true stories, mysteries and what really happened behind the scenes."),
    PlaylistSpec("😂 Funny Buzz", "Funny facts, weird history and the most hilarious things on the planet."),
    PlaylistSpec("🔥 Bee Inspired", "Real words and real stories from great people to fire you up."),
    PlaylistSpec("💰 Money Buzz", "Prices, everyday economics and how big brands really make money, explained."),
    PlaylistSpec("🌍 World Buzz", "Surprising countries, amazing places and travel spots worth the hype."),
    PlaylistSpec("🔍 Buzz Deep Dives", "Our weekly longer videos on the biggest stories."),
    PlaylistSpec("Pending review", "Agent drafts waiting for owner review.", "private"),
    PlaylistSpec("Urgent review", "Time-sensitive agent drafts; review first.", "private"),
    PlaylistSpec("Approved", "Owner-approved drafts; the agent schedules these.", "private"),
    PlaylistSpec("Rejected", "Owner-rejected drafts; never published.", "private"),
]


@dataclass
class Report:
    done: list[str] = field(default_factory=list)

    def add(self, line: str) -> None:
        log.info(line)
        self.done.append(line)


def brand_text() -> tuple[str, str]:
    """Description and keywords, taken from brand/BRAND.md so there is one source of truth."""
    md = (BRAND_DIR / "BRAND.md").read_text(encoding="utf-8")
    description = re.search(r"## Channel description \(About\)\s+```\n(.*?)```", md, re.S).group(1).strip()
    keywords = re.search(r"## Channel keywords\s+```\n(.*?)```", md, re.S).group(1).strip()
    return description, keywords


def _uploads(yt) -> list[dict]:
    uploads = yt.channels().list(part="contentDetails", mine=True).execute()["items"][0][
        "contentDetails"]["relatedPlaylists"]["uploads"]
    ids, token = [], None
    while True:
        resp = yt.playlistItems().list(part="contentDetails", playlistId=uploads, maxResults=50,
                                       pageToken=token).execute()
        ids += [i["contentDetails"]["videoId"] for i in resp["items"]]
        token = resp.get("nextPageToken")
        if not token:
            break
    videos = []
    for i in range(0, len(ids), 50):
        videos += yt.videos().list(part="snippet,status", id=",".join(ids[i:i + 50])).execute()["items"]
    return videos


def make_videos_private(yt, report: Report, dry_run: bool) -> None:
    videos = [v for v in _uploads(yt) if v["status"]["privacyStatus"] != "private"]
    if not videos:
        report.add("videos: all already private")
        return
    if dry_run:
        report.add(f"videos: would make {len(videos)} private")
        return
    log_path = get_settings().data_dir / f"privatized_videos_{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    log_path.write_text(json.dumps(
        [{"id": v["id"], "title": v["snippet"]["title"], "was": v["status"]["privacyStatus"]} for v in videos],
        indent=2, ensure_ascii=False), encoding="utf-8")
    for v in videos:
        status = {k: v["status"][k] for k in _WRITABLE_STATUS if k in v["status"]}
        status["privacyStatus"] = "private"
        status.pop("publishAt", None)
        yt.videos().update(part="status", body={"id": v["id"], "status": status}).execute()
    report.add(f"videos: made {len(videos)} private (undo list: {log_path.name})")


def apply_branding(yt, report: Report, dry_run: bool) -> None:
    channel = yt.channels().list(part="brandingSettings", mine=True).execute()["items"][0]
    description, keywords = brand_text()
    branding = channel.get("brandingSettings", {})
    ch = branding.get("channel", {})
    ch.update({"description": description, "keywords": keywords, "defaultLanguage": "en"})
    if dry_run:
        report.add(f"branding: would set description ({len(description)} chars), keywords, banner")
        return
    banner = yt.channelBanners().insert(
        media_body=MediaFileUpload(str(BRAND_DIR / "banner.png"), mimetype="image/png")).execute()
    body = {"id": channel["id"], "brandingSettings": {"channel": ch, "image": {"bannerExternalUrl": banner["url"]}}}
    yt.channels().update(part="brandingSettings", body=body).execute()
    report.add("branding: description, keywords and banner updated")


def set_watermark(yt, report: Report, dry_run: bool) -> None:
    if dry_run:
        report.add("watermark: would set brand/watermark.png (bottom right, whole video)")
        return
    # Sent directly: the endpoint answers 204 with an empty gzip body, which crashes httplib2's decoder.
    channel_id = yt.channels().list(part="id", mine=True).execute()["items"][0]["id"]
    meta = json.dumps({"timing": {"type": "offsetFromStart", "offsetMs": 0},
                       "position": {"type": "corner", "cornerPosition": "bottomRight"}})
    boundary = "buzzbee-watermark"
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
            f"--{boundary}\r\nContent-Type: image/png\r\n\r\n").encode() \
        + (BRAND_DIR / "watermark.png").read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    resp = AuthorizedSession(credentials()).post(
        "https://www.googleapis.com/upload/youtube/v3/watermarks/set",
        params={"channelId": channel_id, "uploadType": "multipart"},
        data=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"}, timeout=60,
    )
    resp.raise_for_status()
    report.add("watermark: set")


def create_playlists(yt, report: Report, dry_run: bool) -> dict[str, str]:
    existing, token = {}, None
    while True:
        resp = yt.playlists().list(part="snippet,status", mine=True, maxResults=50, pageToken=token).execute()
        existing.update({p["snippet"]["title"]: p for p in resp["items"]})
        token = resp.get("nextPageToken")
        if not token:
            break
    ids = {}
    for spec in PLAYLISTS:
        if spec.title in existing:
            current = existing[spec.title]
            ids[spec.title] = current["id"]
            if current["snippet"].get("description", "") != spec.description:
                if dry_run:
                    report.add(f"playlists: would update description of {spec.title!r}")
                else:
                    yt.playlists().update(part="snippet", body={"id": current["id"], "snippet": {
                        "title": spec.title, "description": spec.description, "defaultLanguage": "en"}}).execute()
                    report.add(f"playlists: updated description of {spec.title!r}")
            continue
        if dry_run:
            report.add(f"playlists: would create {spec.title!r} ({spec.privacy})")
            continue
        created = yt.playlists().insert(part="snippet,status", body={
            "snippet": {"title": spec.title, "description": spec.description, "defaultLanguage": "en"},
            "status": {"privacyStatus": spec.privacy},
        }).execute()
        ids[spec.title] = created["id"]
        report.add(f"playlists: created {spec.title!r} ({spec.privacy})")
    old = [t for t in existing if t not in {s.title for s in PLAYLISTS}]
    if old:
        report.add(f"playlists: left untouched (pre-existing): {', '.join(old)}")
    return ids


def apply_brand(dry_run: bool = True) -> Report:
    yt = data_api()
    report = Report()
    make_videos_private(yt, report, dry_run)
    apply_branding(yt, report, dry_run)
    set_watermark(yt, report, dry_run)
    create_playlists(yt, report, dry_run)
    return report
