"""Podcast discovery: Apple's podcast directory, searched by name or browsed
by category through its public, keyless APIs (the same ones that resolve an
Apple Podcasts link). Only the search words, category and country are sent."""

import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://itunes.apple.com/search"
_LOOKUP_URL = "https://itunes.apple.com/lookup"
_TOP_URL = "https://itunes.apple.com/{country}/rss/toppodcasts/limit={limit}/genre={genre}/json"
_TIMEOUT = 15
MAX_RESULTS = 50

# Apple's top-level podcast categories (genre ids).
CATEGORIES: Dict[int, str] = {
    1301: "Arts",
    1321: "Business",
    1303: "Comedy",
    1304: "Education",
    1483: "Fiction",
    1511: "Government",
    1512: "Health & Fitness",
    1487: "History",
    1305: "Kids & Family",
    1502: "Leisure",
    1310: "Music",
    1489: "News",
    1314: "Religion & Spirituality",
    1533: "Science",
    1324: "Society & Culture",
    1545: "Sports",
    1309: "TV & Film",
    1318: "Technology",
    1488: "True Crime",
}


class DirectoryError(RuntimeError):
    """The directory couldn't be reached or answered something unreadable."""


def _get(url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    try:
        resp = requests.get(url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        raise DirectoryError(f"The podcast directory didn't answer: {e}") from e


def _show(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A directory entry as P3 shows it; None without a feed (P3 can't fetch
    it)."""
    feed = item.get("feedUrl")
    if not feed:
        return None
    return {
        "name": item.get("collectionName") or item.get("trackName") or feed,
        "author": item.get("artistName"),
        "feed_url": feed,
        "artwork": item.get("artworkUrl100") or item.get("artworkUrl60"),
        "genre": item.get("primaryGenreName"),
        "apple_url": item.get("collectionViewUrl"),
        "episode_count": item.get("trackCount"),
    }


def _country(country: str) -> str:
    country = (country or "us").lower()
    return country if len(country) == 2 and country.isalpha() else "us"


def search(
    term: str, country: str = "us", genre: Optional[int] = None, limit: int = 30
) -> List[Dict[str, Any]]:
    """Podcasts matching ``term``, in ``genre`` when given."""
    data = _get(
        _SEARCH_URL,
        {
            "media": "podcast",
            "entity": "podcast",
            "term": term,
            "country": _country(country),
            "limit": MAX_RESULTS if genre else min(limit, MAX_RESULTS),
        },
    )
    shows = []
    for item in data.get("results") or []:
        if genre and str(genre) not in (item.get("genreIds") or []):
            continue
        show = _show(item)
        if show:
            shows.append(show)
    return shows[:limit]


def top(genre: int, country: str = "us", limit: int = 30) -> List[Dict[str, Any]]:
    """The directory's most popular podcasts in a category, in chart order."""
    limit = min(limit, MAX_RESULTS)
    chart = _get(_TOP_URL.format(country=_country(country), limit=limit, genre=genre))
    entries = (chart.get("feed") or {}).get("entry") or []
    if isinstance(entries, dict):  # a chart of one
        entries = [entries]
    ids = [
        (e.get("id") or {}).get("attributes", {}).get("im:id")
        for e in entries
        if isinstance(e, dict)
    ]
    ids = [i for i in ids if i]
    if not ids:
        return []
    found = _get(
        _LOOKUP_URL,
        {"id": ",".join(ids), "entity": "podcast", "country": _country(country)},
    )
    by_id = {str(item.get("collectionId")): item for item in found.get("results") or []}
    shows = [_show(by_id[i]) for i in ids if i in by_id]
    return [s for s in shows if s]
