"""Podcast episode downloader and RSS feed processor."""

import hashlib
import logging
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

import feedparser
import requests

from .database import P3Database

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2  # seconds


def _retry_request(method: str, url: str, **kwargs) -> requests.Response:
    """Execute an HTTP request with retry and exponential backoff."""
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except (requests.RequestException, requests.HTTPError) as e:
            if attempt == MAX_RETRIES - 1:
                raise
            wait = RETRY_BACKOFF_BASE ** (attempt + 1)
            logger.warning(
                "Request to %s failed (attempt %d/%d): %s. Retrying in %ds...",
                url,
                attempt + 1,
                MAX_RETRIES,
                e,
                wait,
            )
            time.sleep(wait)


def _safe_filename(title: str, max_length: int = 50) -> str:
    """Generate a filesystem-safe filename from a title.

    Strips non-alphanumeric characters (keeping spaces, hyphens, underscores),
    collapses whitespace, and enforces a maximum length. Returns a fallback
    name if the result is empty.
    """
    cleaned = re.sub(r"[^\w\s-]", "", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        cleaned = "untitled"
    return cleaned[:max_length]


class PodcastDownloader:
    def __init__(
        self,
        db: P3Database,
        data_dir: str = "data",
        max_episodes: int = 10,
        audio_format: str = "wav",
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ):
        self.db = db
        self.data_dir = Path(data_dir)
        self.audio_dir = self.data_dir / "audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.max_episodes = max_episodes
        self.audio_format = audio_format
        self.progress_callback = progress_callback

    def add_feed(self, name: str, url: str, category: str = None) -> int:
        """Add a new podcast feed to the database."""
        existing = self.db.get_podcast_by_url(url)
        if existing:
            return existing["id"]
        return self.db.add_podcast(name, url, category)

    def fetch_episodes(self, rss_url: str, limit: int = None) -> List[Dict]:
        """Fetch episode metadata from RSS feed."""
        if limit is None:
            limit = self.max_episodes

        try:
            feed = feedparser.parse(rss_url)
            episodes = []

            for entry in feed.entries[:limit]:
                # Find audio enclosure
                audio_url = None
                for enclosure in entry.get("enclosures", []):
                    if enclosure.type and "audio" in enclosure.type:
                        audio_url = enclosure.href
                        break

                if not audio_url:
                    continue

                # Parse publication date
                pub_date = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    pub_date = datetime(
                        *entry.published_parsed[:6], tzinfo=timezone.utc
                    )
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    pub_date = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)

                episodes.append(
                    {
                        "title": entry.get("title", "Unknown Title"),
                        "url": audio_url,
                        "date": pub_date,
                        "description": entry.get("description", ""),
                        "guid": entry.get("id", audio_url),
                    }
                )

            return episodes

        except Exception as e:
            logger.error("Error fetching RSS feed %s: %s", rss_url, e)
            return []

    def download_episode(self, episode_url: str, filename: str) -> Optional[str]:
        """Download and normalize audio episode."""
        tmp_path = None
        try:
            response = _retry_request("GET", episode_url, stream=True, timeout=300)

            # Save to temporary file first
            with tempfile.NamedTemporaryFile(delete=False, suffix=".tmp") as tmp_file:
                for chunk in response.iter_content(chunk_size=8192):
                    tmp_file.write(chunk)
                tmp_path = tmp_file.name

            # Convert and normalize with ffmpeg into a staging file, renamed
            # only on success, so an interrupted run never leaves a truncated
            # file at the final path (whose existence marks completed work).
            output_path = self.audio_dir / f"{filename}.{self.audio_format}"
            staging_path = output_path.parent / f"{output_path.name}.partial"

            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                tmp_path,
                "-ar",
                "16000",  # 16kHz sample rate for Whisper/Parakeet
                "-ac",
                "1",  # mono
                "-c:a",
                "pcm_s16le" if self.audio_format == "wav" else "libmp3lame",
                "-af",
                "loudnorm",  # normalize audio levels
                "-f",
                self.audio_format,  # explicit muxer: the .partial suffix hides it
                str(staging_path),
            ]

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                logger.warning("FFmpeg normalization failed: %s", result.stderr)
                return self._fallback_conversion(tmp_path, output_path)

            os.replace(staging_path, output_path)
            return str(output_path)

        except Exception as e:
            logger.error("Error downloading %s: %s", episode_url, e)
            return None

        finally:
            # Always clean up temp file
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def _fallback_conversion(self, input_path: str, output_path: Path) -> Optional[str]:
        """Fallback audio conversion using ffmpeg without normalization."""
        staging_path = output_path.parent / f"{output_path.name}.partial"
        try:
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                input_path,
                "-ar",
                "16000",
                "-ac",
                "1",
                "-f",
                output_path.suffix.lstrip(
                    "."
                ),  # explicit muxer: the .partial suffix hides it
                str(staging_path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode == 0:
                os.replace(staging_path, output_path)
                return str(output_path)
            else:
                logger.error("Fallback conversion failed: %s", result.stderr)
                return None

        except Exception as e:
            logger.error("Fallback conversion failed: %s", e)
            return None
        finally:
            if staging_path.exists():
                try:
                    staging_path.unlink()
                except OSError:
                    pass

    def process_feed(self, rss_url: str) -> int:
        """Process a single RSS feed and download new episodes."""
        podcast = self.db.get_podcast_by_url(rss_url)
        if not podcast:
            logger.error("Podcast not found for URL: %s", rss_url)
            return 0

        episodes = self.fetch_episodes(rss_url)
        total = len(episodes)
        downloaded_count = 0

        for i, ep_data in enumerate(episodes):
            # Skip if episode already exists
            if self.db.episode_exists(ep_data["url"]):
                continue

            # Deterministic filename per episode, so audio already downloaded
            # and normalized by an interrupted earlier run is reused rather
            # than fetched and converted again.
            safe_title = _safe_filename(ep_data["title"])
            url_hash = hashlib.sha1(ep_data["url"].encode()).hexdigest()[:10]
            filename = f"{podcast['id']}_{safe_title}_{url_hash}"
            output_path = self.audio_dir / f"{filename}.{self.audio_format}"

            if output_path.exists() and output_path.stat().st_size > 0:
                logger.info("Reusing existing audio file for: %s", ep_data["title"])
                file_path = str(output_path)
            else:
                logger.info("Downloading: %s", ep_data["title"])
                if self.progress_callback:
                    self.progress_callback(i, total, f"Downloading: {ep_data['title']}")
                file_path = self.download_episode(ep_data["url"], filename)

            if file_path:
                self.db.add_episode(
                    podcast_id=podcast["id"],
                    title=ep_data["title"],
                    date=ep_data["date"],
                    url=ep_data["url"],
                    file_path=file_path,
                )
                downloaded_count += 1
                logger.info("Downloaded: %s", ep_data["title"])
                if self.progress_callback:
                    self.progress_callback(
                        i + 1, total, f"Downloaded: {ep_data['title']}"
                    )
            else:
                logger.warning("Failed to download: %s", ep_data["title"])
                if self.progress_callback:
                    self.progress_callback(i + 1, total, f"Failed: {ep_data['title']}")

        return downloaded_count

    def fetch_all_feeds(self, feeds_config: List[Dict]) -> Dict[str, int]:
        """Process all configured RSS feeds."""
        results = {}

        for feed_config in feeds_config:
            name = feed_config["name"]
            url = feed_config["url"]
            category = feed_config.get("category")

            logger.info("Processing feed: %s", name)

            # Ensure podcast exists in database
            self.add_feed(name, url, category)

            # Process episodes
            count = self.process_feed(url)
            results[name] = count

            logger.info("Downloaded %d new episodes from %s", count, name)

        return results
