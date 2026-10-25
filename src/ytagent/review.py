"""Approval flow on YouTube: you move a private Short from "Pending review" to "Approved" (or "Rejected")
in YouTube Studio; this sync publishes approved Shorts at the next best time slot.

Run every ~10 minutes by the autopilot. Quota: ~4 units per run plus 50 per scheduled video.
The agent never deletes videos; it only moves playlist entries and changes visibility/schedule.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ytagent.config import get_settings
from ytagent.youtube import data_api

log = logging.getLogger(__name__)

PENDING, APPROVED, REJECTED = "Pending review", "Approved", "Rejected"
_WRITABLE_STATUS = ("privacyStatus", "embeddable", "license", "publicStatsViewable", "publishAt",
                    "selfDeclaredMadeForKids", "containsSyntheticMedia")


def next_slot(now: datetime, slots: list[str], taken: list[datetime], tz: str, min_gap_h: float,
              lead_min: int = 20) -> datetime:
    """The next daily slot (local "HH:MM") at least `lead_min` ahead and `min_gap_h` away from taken times."""
    zone = ZoneInfo(tz)
    local_now = now.astimezone(zone)
    for day in range(0, 14):
        date = (local_now + timedelta(days=day)).date()
        for slot in sorted(slots):
            hh, mm = (int(x) for x in slot.split(":"))
            when = datetime(date.year, date.month, date.day, hh, mm, tzinfo=zone)
            if when < local_now + timedelta(minutes=lead_min):
                continue
            if all(abs((when - t).total_seconds()) >= min_gap_h * 3600 for t in taken):
                return when.astimezone(UTC)
    return (now + timedelta(hours=1)).astimezone(UTC)


def _playlists(yt) -> dict[str, str]:
    resp = yt.playlists().list(part="snippet", mine=True, maxResults=50).execute()
    return {p["snippet"]["title"]: p["id"] for p in resp["items"]}


def _items(yt, playlist_id: str) -> list[dict]:
    items, token = [], None
    while True:
        resp = yt.playlistItems().list(part="snippet,contentDetails", playlistId=playlist_id, maxResults=50,
                                       pageToken=token).execute()
        items += resp["items"]
        token = resp.get("nextPageToken")
        if not token:
            return items


def _remove_from(yt, playlist_items: list[dict], video_id: str) -> None:
    """Take a video out of a playlist (the video itself is untouched) and out of our local copy of it."""
    for item in [i for i in playlist_items if i["contentDetails"]["videoId"] == video_id]:
        yt.playlistItems().delete(id=item["id"]).execute()
        playlist_items.remove(item)


def _add_to(yt, playlist_id: str | None, video_id: str) -> None:
    if not playlist_id:
        log.warning("topic playlist missing on the channel; run `ytagent brand apply --yes` to create it")
        return
    yt.playlistItems().insert(part="snippet", body={"snippet": {
        "playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}).execute()


def _video_status(yt, video_ids: list[str]) -> dict[str, dict]:
    out = {}
    for i in range(0, len(video_ids), 50):
        resp = yt.videos().list(part="status", id=",".join(video_ids[i:i + 50])).execute()
        out.update({v["id"]: v["status"] for v in resp["items"]})
    return out


def sync() -> dict:
    """Process Approved / Rejected playlists and catch Shorts published manually or by schedule."""
    from ytagent.db import session_scope
    from ytagent.models import Video
    from ytagent.publish import PLAYLIST

    cfg = get_settings().publishing
    yt = data_api()
    lists = _playlists(yt)
    if APPROVED not in lists:
        return {"error": "no 'Approved' playlist on the channel"}
    pending = _items(yt, lists[PENDING]) if PENDING in lists else []
    approved = _items(yt, lists[APPROVED])
    rejected = _items(yt, lists[REJECTED]) if REJECTED in lists else []
    report = {"scheduled": [], "published_now": [], "rejected": [], "went_live": []}
    now = datetime.now(UTC)

    with session_scope() as s:
        by_yt = {v.youtube_id: v for v in s.scalars(select(Video).where(Video.youtube_id.is_not(None)))}
        taken = [v.publish_at for v in by_yt.values() if v.status == "scheduled" and v.publish_at]
        ids = list({i["contentDetails"]["videoId"] for i in pending + approved + rejected} | set(by_yt))
        statuses = _video_status(yt, ids) if ids else {}

        for item in approved:
            vid = item["contentDetails"]["videoId"]
            status = statuses.get(vid)
            row = by_yt.get(vid)
            if status is None:  # deleted on YouTube
                _remove_from(yt, approved, vid)
                continue
            if status["privacyStatus"] == "public" or status.get("publishAt"):
                _remove_from(yt, approved, vid)  # already live or already scheduled (e.g. by you)
                _remove_from(yt, pending, vid)
                continue
            body = {k: status[k] for k in _WRITABLE_STATUS if k in status}
            if cfg.publish_mode == "now":
                body.pop("publishAt", None)
                body["privacyStatus"] = "public"
                when = now
            else:
                when = next_slot(now, cfg.slots, taken, cfg.timezone, cfg.min_gap_hours)
                body.update({"privacyStatus": "private", "publishAt": when.isoformat().replace("+00:00", "Z")})
                taken.append(when)
            yt.videos().update(part="status", body={"id": vid, "status": body}).execute()
            pillar = (row.script or {}).get("pillar") if row else None
            _add_to(yt, lists.get(PLAYLIST.get(pillar, "")), vid)
            _remove_from(yt, approved, vid)
            _remove_from(yt, pending, vid)
            if row:
                row.status = "published" if cfg.publish_mode == "now" else "scheduled"
                row.publish_at = when
            title = row.title if row else item["snippet"]["title"]
            key = "published_now" if cfg.publish_mode == "now" else "scheduled"
            report[key].append(f"{title} @ {when.astimezone(ZoneInfo(cfg.timezone)):%a %d %b %H:%M}")

        for item in rejected:
            vid = item["contentDetails"]["videoId"]
            row = by_yt.get(vid)
            if row and row.status not in ("rejected", "published"):
                row.status = "rejected"
                report["rejected"].append(row.title)
            _remove_from(yt, pending, vid)

        # Shorts that went live (scheduled time passed, or you published one yourself).
        for vid, row in by_yt.items():
            st = statuses.get(vid)
            if st and st["privacyStatus"] == "public" and row.status in ("pending_review", "scheduled"):
                row.status = "published"
                report["went_live"].append(row.title)
                _remove_from(yt, pending, vid)
    return {k: v for k, v in report.items() if v}
