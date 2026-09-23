"""YouTube sources: URL parsing, listing and audio download via yt-dlp.

A YouTube source is either a channel (new uploads are fetched like a feed)
or a single standalone video. Playlists are not stored as sources; they are
expanded into one standalone video source per entry.

Shorts and livestreams (live, upcoming and archived streams) are skipped.
Premieres count as ordinary uploads once they have aired.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

logger = logging.getLogger(__name__)
_ytdlp_logger = logging.getLogger(__name__ + ".ytdlp")

SOURCE_RSS = "rss"
SOURCE_CHANNEL = "youtube_channel"
SOURCE_VIDEO = "youtube_video"

KIND_CHANNEL = "channel"
KIND_VIDEO = "video"
KIND_PLAYLIST = "playlist"

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_HANDLE_RE = re.compile(r"^@[\w.\-·]+$")
_NAME_RE = re.compile(r"^[\w.\-]+$")

# live_status values (from yt-dlp) that mark a livestream in any phase.
_LIVE_STATUSES = {"is_live", "is_upcoming", "post_live", "was_live"}
# availability values (from yt-dlp) that need an account or payment.
_RESTRICTED_AVAILABILITY = {
    "private",
    "premium_only",
    "subscriber_only",
    "needs_auth",
}
# Titles YouTube uses for playlist entries that can no longer be played.
_UNAVAILABLE_TITLES = {"[private video]", "[deleted video]"}

# Channel listings are scanned a little past the episode limit so that a
# few filtered-out entries (premieres not yet aired, members-only uploads)
# do not starve the fetch.
_LISTING_SLACK = 10


class YouTubeError(Exception):
    """A YouTube lookup or download failed."""


@dataclass(frozen=True)
class YouTubeRef:
    """A parsed YouTube URL. ``url`` is the canonical form."""

    kind: str
    id: str
    url: str


def is_youtube_url(url: str) -> bool:
    host = (urlparse(_with_scheme(url.strip())).hostname or "").lower()
    return (
        host == "youtube.com"
        or host.endswith(".youtube.com")
        or host in ("youtu.be", "www.youtu.be")
    )


def video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def channel_url(channel_id: str) -> str:
    return f"https://www.youtube.com/channel/{channel_id}"


def parse_youtube_url(url: str) -> YouTubeRef:
    """Classify a YouTube URL as a channel, video or playlist.

    A watch URL that also carries ``list=`` is treated as the single video.
    Raises ``ValueError`` for Shorts and for URLs that are not a channel,
    video or playlist.
    """
    parsed = urlparse(_with_scheme(url.strip()))
    host = (parsed.hostname or "").lower()
    parts = [unquote(p) for p in parsed.path.split("/") if p]
    query = parse_qs(parsed.query)

    if host in ("youtu.be", "www.youtu.be"):
        if parts and _VIDEO_ID_RE.match(parts[0]):
            return YouTubeRef(KIND_VIDEO, parts[0], video_url(parts[0]))
        raise ValueError(f"Unsupported YouTube URL: {url}")

    if not parts:
        raise ValueError(f"Unsupported YouTube URL: {url}")
    head = parts[0]

    if head == "watch":
        vid = (query.get("v") or [""])[0]
        if _VIDEO_ID_RE.match(vid):
            return YouTubeRef(KIND_VIDEO, vid, video_url(vid))
        raise ValueError(f"No video id in YouTube URL: {url}")

    if head == "feeds" and parts[1:] == ["videos.xml"]:
        # YouTube's own RSS feed for a channel or playlist.
        cid = (query.get("channel_id") or [""])[0]
        if _CHANNEL_ID_RE.match(cid):
            return YouTubeRef(KIND_CHANNEL, cid, channel_url(cid))
        pid = (query.get("playlist_id") or [""])[0]
        if _PLAYLIST_ID_RE.match(pid):
            return YouTubeRef(
                KIND_PLAYLIST, pid, f"https://www.youtube.com/playlist?list={pid}"
            )
        raise ValueError(f"Unsupported YouTube feed URL: {url}")

    if head == "shorts":
        raise ValueError("YouTube Shorts are not supported")

    if head in ("live", "embed", "v") and len(parts) > 1:
        if _VIDEO_ID_RE.match(parts[1]):
            return YouTubeRef(KIND_VIDEO, parts[1], video_url(parts[1]))
        raise ValueError(f"Unsupported YouTube URL: {url}")

    if head == "playlist":
        pid = (query.get("list") or [""])[0]
        if _PLAYLIST_ID_RE.match(pid):
            return YouTubeRef(
                KIND_PLAYLIST, pid, f"https://www.youtube.com/playlist?list={pid}"
            )
        raise ValueError(f"No playlist id in YouTube URL: {url}")

    if head == "channel" and len(parts) > 1 and _CHANNEL_ID_RE.match(parts[1]):
        return YouTubeRef(KIND_CHANNEL, parts[1], channel_url(parts[1]))

    if head.startswith("@") and _HANDLE_RE.match(head):
        return YouTubeRef(KIND_CHANNEL, head, f"https://www.youtube.com/{head}")

    if head in ("c", "user") and len(parts) > 1 and _NAME_RE.match(parts[1]):
        return YouTubeRef(
            KIND_CHANNEL, parts[1], f"https://www.youtube.com/{head}/{parts[1]}"
        )

    raise ValueError(f"Unsupported YouTube URL: {url}")


def _with_scheme(url: str) -> str:
    return url if "://" in url else f"https://{url}"


# ----------------------------------------------------------------------
# Filtering
# ----------------------------------------------------------------------


def flat_entry_rejection(entry: Dict[str, Any]) -> Optional[str]:
    """Reason to skip a flat channel/playlist entry, or None to keep it.

    Flat entries carry only what the listing page shows, so this catches
    what is visible there; ``video_rejection`` makes the final decision on
    full metadata.
    """
    if "/shorts/" in (entry.get("url") or ""):
        return "YouTube Short"
    if entry.get("live_status") in _LIVE_STATUSES:
        return "livestream"
    if entry.get("availability") in _RESTRICTED_AVAILABILITY:
        return f"not publicly available ({entry['availability']})"
    if (entry.get("title") or "").strip().lower() in _UNAVAILABLE_TITLES:
        return "private or deleted"
    return None


def video_rejection(info: Dict[str, Any]) -> Optional[str]:
    """Reason to skip a video given its full yt-dlp metadata, or None."""
    if info.get("media_type") == "short":
        return "YouTube Short"
    if (
        info.get("media_type") == "livestream"
        or info.get("live_status") in _LIVE_STATUSES
    ):
        return "livestream"
    if info.get("availability") in _RESTRICTED_AVAILABILITY:
        return f"not publicly available ({info['availability']})"
    return None


def upload_datetime(info: Dict[str, Any]) -> Optional[datetime]:
    """Publication time in UTC from yt-dlp metadata, or None."""
    for key in ("timestamp", "release_timestamp"):
        ts = info.get(key)
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts, tz=timezone.utc)
    upload_date = info.get("upload_date")
    if isinstance(upload_date, str) and len(upload_date) == 8:
        try:
            return datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


# ----------------------------------------------------------------------
# yt-dlp access
# ----------------------------------------------------------------------


def _ydl(extra: Optional[Dict[str, Any]] = None):
    """A configured ``yt_dlp.YoutubeDL``. Imported lazily so the rest of the
    app does not pay for loading yt-dlp."""
    import yt_dlp

    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "logger": _ytdlp_logger,
        "socket_timeout": 30,
    }
    if extra:
        opts.update(extra)
    return yt_dlp.YoutubeDL(opts)


def _extract(url: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run ``extract_info(download=False)``; yt-dlp errors become YouTubeError."""
    from yt_dlp.utils import YoutubeDLError

    try:
        with _ydl(extra) as ydl:
            info = ydl.extract_info(url, download=False)
    except YoutubeDLError as e:
        raise YouTubeError(_clean_error(e)) from e
    if not info:
        raise YouTubeError(f"No information returned for {url}")
    return info


def _clean_error(e: Exception) -> str:
    # yt-dlp prefixes messages with a coloured "ERROR: "; keep the text only.
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
    return msg.removeprefix("ERROR: ").strip()


def resolve_channel(url: str) -> tuple[str, str]:
    """Resolve any channel URL (handle, /c/, /user/, /channel/) to its
    canonical ``/channel/UC...`` URL and display name."""
    ref = parse_youtube_url(url)
    if ref.kind != KIND_CHANNEL:
        raise ValueError(f"Not a YouTube channel URL: {url}")
    info = _extract(
        f"{ref.url}/videos",
        {"extract_flat": "in_playlist", "playlist_items": "1"},
    )
    channel_id = info.get("channel_id")
    if not channel_id and _CHANNEL_ID_RE.match(str(info.get("id") or "")):
        channel_id = info["id"]
    if not channel_id:
        raise YouTubeError(f"Could not determine the channel id for {url}")
    name = info.get("channel") or info.get("uploader") or channel_id
    return channel_url(channel_id), name


def _flat_entries(info: Dict[str, Any]) -> List[Dict[str, Any]]:
    entries = []
    for entry in info.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        vid = entry.get("id")
        if not isinstance(vid, str) or not _VIDEO_ID_RE.match(vid):
            continue  # nested playlists or malformed rows
        entries.append(entry)
    return entries


def list_channel_videos(channel: str, limit: int) -> List[Dict[str, Any]]:
    """Newest regular uploads of a channel as flat entries, newest first.

    Only the channel's Videos tab is read, which leaves out the Shorts and
    Live tabs; entries that still look like a Short or stream are dropped.
    At most ``limit`` entries are returned.
    """
    ref = parse_youtube_url(channel)
    if ref.kind != KIND_CHANNEL:
        raise ValueError(f"Not a YouTube channel URL: {channel}")
    scan = max(limit, 1) + _LISTING_SLACK
    info = _extract(
        f"{ref.url}/videos",
        {"extract_flat": "in_playlist", "playlist_items": f"1:{scan}"},
    )
    kept = []
    for entry in _flat_entries(info):
        reason = flat_entry_rejection(entry)
        if reason:
            logger.info("Skipping %s: %s", entry.get("id"), reason)
            continue
        kept.append(entry)
        if len(kept) >= limit:
            break
    return kept


def list_playlist_videos(
    playlist: str,
) -> tuple[str, List[Dict[str, Any]], int]:
    """Title, flat entries and number of entries dropped for a playlist.

    Shorts, streams and unavailable entries are dropped (and counted);
    duplicates are removed silently."""
    ref = parse_youtube_url(playlist)
    if ref.kind != KIND_PLAYLIST:
        raise ValueError(f"Not a YouTube playlist URL: {playlist}")
    info = _extract(ref.url, {"extract_flat": "in_playlist"})
    kept: List[Dict[str, Any]] = []
    seen = set()
    dropped = 0
    for entry in _flat_entries(info):
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])
        reason = flat_entry_rejection(entry)
        if reason:
            logger.info("Skipping %s: %s", entry["id"], reason)
            dropped += 1
            continue
        kept.append(entry)
    return info.get("title") or ref.id, kept, dropped


def get_video_info(url: str) -> Dict[str, Any]:
    """Full metadata for one video (no download), with audio-only format
    selection already applied."""
    ref = parse_youtube_url(url)
    if ref.kind != KIND_VIDEO:
        raise ValueError(f"Not a YouTube video URL: {url}")
    return _extract(ref.url, _download_opts())


def _download_opts(outdir: Optional[str] = None) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "format": "bestaudio/best",
        "noplaylist": True,
    }
    if outdir:
        opts["outtmpl"] = {"default": str(Path(outdir) / "%(id)s.%(ext)s")}
    return opts


def download_audio(info: Dict[str, Any], outdir: str) -> str:
    """Download the audio of a video whose metadata came from
    ``get_video_info`` into ``outdir``; returns the downloaded file's path.

    Reuses the metadata the way ``yt-dlp --load-info-json`` does, so the
    video page is not fetched a second time.
    """
    from yt_dlp.utils import YoutubeDLError

    try:
        with _ydl(_download_opts(outdir)) as ydl:
            try:
                ydl.process_ie_result(
                    ydl.sanitize_info(info, remove_private_keys=True), download=True
                )
            except YoutubeDLError as e:
                # Same fallback as --load-info-json: stale metadata (expired
                # format URLs) is retried from the video page.
                page = info.get("webpage_url")
                if not page:
                    raise
                logger.warning("Retrying %s from its page: %s", page, e)
                ydl.download([page])
    except YoutubeDLError as e:
        raise YouTubeError(_clean_error(e)) from e

    files = [
        p
        for p in Path(outdir).iterdir()
        if p.is_file() and p.suffix not in (".part", ".ytdl")
    ]
    if not files:
        raise YouTubeError(f"yt-dlp produced no file for {info.get('id')}")
    return str(max(files, key=lambda p: p.stat().st_size))
