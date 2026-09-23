"""Resolve source URLs: podcast feeds, Apple Podcasts links and YouTube."""

import logging
import re
from dataclasses import dataclass

import requests

from p3 import youtube

logger = logging.getLogger(__name__)

_APPLE_PODCAST_RE = re.compile(r"podcasts\.apple\.com/.*id(\d+)")


@dataclass(frozen=True)
class ResolvedSource:
    """A source ready to store: ``url`` goes in ``podcasts.rss_url``."""

    url: str
    name: str | None
    source_type: str


def is_youtube_playlist(url: str) -> bool:
    """True for a YouTube playlist URL, which is imported as one standalone
    source per video rather than stored as a source itself."""
    if not youtube.is_youtube_url(url):
        return False
    try:
        return youtube.parse_youtube_url(url).kind == youtube.KIND_PLAYLIST
    except ValueError:
        return False


def resolve_source(url: str) -> ResolvedSource:
    """Resolve a feed, Apple Podcasts, YouTube channel or YouTube video URL.

    YouTube videos are checked against their full metadata here, so a Short
    or livestream is refused before a source is created for it.

    Raises ``ValueError`` when the URL is recognised but cannot be used, and
    ``youtube.YouTubeError`` when a YouTube lookup fails.
    """
    url = url.strip()
    if not youtube.is_youtube_url(url):
        rss_url, name = resolve_podcast_url(url)
        return ResolvedSource(rss_url, name, youtube.SOURCE_RSS)

    ref = youtube.parse_youtube_url(url)
    if ref.kind == youtube.KIND_CHANNEL:
        canonical, name = youtube.resolve_channel(ref.url)
        return ResolvedSource(canonical, name, youtube.SOURCE_CHANNEL)
    if ref.kind == youtube.KIND_VIDEO:
        info = youtube.get_video_info(ref.url)
        reason = youtube.video_rejection(info)
        if reason:
            raise ValueError(f"This video can't be added: {reason}")
        return ResolvedSource(ref.url, info.get("title"), youtube.SOURCE_VIDEO)
    raise ValueError("Playlists are imported as one source per video")


def resolve_podcast_url(url: str) -> tuple[str, str | None]:
    """Resolve a podcast URL to an RSS feed URL.

    Accepts RSS feeds (returned as-is) and Apple Podcasts links.
    Returns ``(rss_url, podcast_name)``.  *podcast_name* is ``None``
    when the input is already an RSS feed.

    Raises ``ValueError`` when the URL is recognised but cannot be resolved.
    """
    url = url.strip()

    m = _APPLE_PODCAST_RE.search(url)
    if m:
        return _resolve_apple(m.group(1))

    # Treat everything else as a direct RSS feed URL
    return url, None


def _resolve_apple(podcast_id: str) -> tuple[str, str]:
    """Call the iTunes Lookup API to get the RSS feed for a podcast ID."""
    resp = requests.get(
        f"https://itunes.apple.com/lookup?id={podcast_id}&entity=podcast",
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    for result in data.get("results", []):
        feed_url = result.get("feedUrl")
        name = result.get("collectionName", "Unknown Podcast")
        if feed_url:
            logger.info(
                "Resolved Apple Podcasts id=%s -> %s (%s)",
                podcast_id,
                feed_url,
                name,
            )
            return feed_url, name

    raise ValueError(f"No RSS feed found for Apple Podcasts id {podcast_id}")
