"""Resolve podcast URLs (Apple Podcasts, etc.) to RSS feed URLs."""

import logging
import re

import requests

logger = logging.getLogger(__name__)

_APPLE_PODCAST_RE = re.compile(r"podcasts\.apple\.com/.*id(\d+)")


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
                podcast_id, feed_url, name,
            )
            return feed_url, name

    raise ValueError(f"No RSS feed found for Apple Podcasts id {podcast_id}")
