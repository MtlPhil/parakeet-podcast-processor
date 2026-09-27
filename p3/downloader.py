"""Podcast episode downloader and RSS feed processor."""

import calendar
import hashlib
import logging
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

import feedparser
import requests

from . import youtube
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
    raise AssertionError("unreachable: the final attempt re-raises")


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

    def add_feed(
        self,
        name: str,
        url: str,
        category: Optional[str] = None,
        source_type: str = youtube.SOURCE_RSS,
    ) -> int:
        """Add a new source to the database, or return the existing one's id."""
        existing = self.db.get_podcast_by_url(url)
        if existing:
            return existing["id"]
        return self.db.add_podcast(name, url, category, source_type)

    def fetch_episodes(self, rss_url: str, limit: Optional[int] = None) -> List[Dict]:
        """Fetch episode metadata from an RSS feed or a YouTube source.

        YouTube listings are flat: ``date`` is None, and a single-video
        source is returned without a network call (its title is the URL).
        YouTube errors propagate as ``youtube.YouTubeError``.
        """
        if limit is None:
            limit = self.max_episodes

        if youtube.is_youtube_url(rss_url):
            return self._youtube_listing(rss_url, limit)

        return self._rss_episodes(rss_url)[:limit]

    def _rss_episodes(self, rss_url: str) -> List[Dict]:
        """Every episode in an RSS feed, newest-as-listed first, unfiltered
        by any episode limit."""
        try:
            feed = feedparser.parse(rss_url)
            episodes = []

            for entry in feed.entries:
                # Find audio enclosure
                audio_url = None
                for enclosure in entry.get("enclosures", []):
                    if enclosure.type and "audio" in enclosure.type:
                        audio_url = enclosure.href
                        break

                if not audio_url:
                    continue

                # Publication date; feedparser normalizes both fields to UTC.
                parsed = entry.get("published_parsed") or entry.get("updated_parsed")
                pub_date = (
                    datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
                    if parsed
                    else None
                )

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

    def list_preview_episodes(self, url: str, months: int = 12) -> List[Dict]:
        """Every episode available for hand-picking when a source is first
        added.

        RSS feeds return every entry. YouTube channels return uploads from
        the last ``months`` months only, newest first (a channel's full
        history can run into the thousands, and each candidate needs its
        own lookup to date it -- see ``youtube.list_channel_videos_since``).
        A single YouTube video returns just itself. Raises ``ValueError``
        for a playlist, which is imported as one source per video instead
        of picked from.
        """
        if not youtube.is_youtube_url(url):
            return self._rss_episodes(url)

        ref = youtube.parse_youtube_url(url)
        if ref.kind == youtube.KIND_VIDEO:
            return self._youtube_listing(url, 1)
        if ref.kind == youtube.KIND_PLAYLIST:
            raise ValueError("Playlists are imported as one source per video")

        since = datetime.now(timezone.utc) - timedelta(days=30 * months)
        entries = youtube.list_channel_videos_since(ref.url, since)
        return [
            {
                "title": e.get("title") or youtube.video_url(e["id"]),
                "url": youtube.video_url(e["id"]),
                "date": e.get("date"),
                "description": e.get("description") or "",
                "guid": e["id"],
            }
            for e in entries
        ]

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

            return self._normalize(tmp_path, filename)

        except Exception as e:
            logger.error("Error downloading %s: %s", episode_url, e)
            return None

        finally:
            # Always clean up temp file
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def _normalize(self, input_path: str, filename: str) -> Optional[str]:
        """Convert a downloaded media file to normalized 16 kHz mono audio at
        ``audio_dir/filename.<audio_format>``. Returns the path, or None."""
        # Convert and normalize with ffmpeg into a staging file, renamed
        # only on success, so an interrupted run never leaves a truncated
        # file at the final path (whose existence marks completed work).
        output_path = self.audio_dir / f"{filename}.{self.audio_format}"
        staging_path = output_path.parent / f"{output_path.name}.partial"

        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-vn",  # audio only (YouTube downloads may carry video)
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
            return self._fallback_conversion(input_path, output_path)

        os.replace(staging_path, output_path)
        return str(output_path)

    def _fallback_conversion(self, input_path: str, output_path: Path) -> Optional[str]:
        """Fallback audio conversion using ffmpeg without normalization."""
        staging_path = output_path.parent / f"{output_path.name}.partial"
        try:
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                input_path,
                "-vn",
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

    def process_feed(
        self, rss_url: str, episode_guids: Optional[List[str]] = None
    ) -> int:
        """Process a single RSS feed and download new episodes.

        If ``episode_guids`` is given (hand-picked from a preview listing),
        only those episodes are downloaded, regardless of ``max_episodes``;
        otherwise the usual top-N-by-feed-order behavior applies.
        """
        podcast = self.db.get_podcast_by_url(rss_url)
        if not podcast:
            logger.error("Podcast not found for URL: %s", rss_url)
            return 0

        if youtube.is_youtube_url(rss_url):
            return self._process_youtube_source(podcast, episode_guids)

        if episode_guids is not None:
            wanted = set(episode_guids)
            episodes = [e for e in self._rss_episodes(rss_url) if e["guid"] in wanted]
        else:
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

            file_path: Optional[str]
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
        """Process all configured feeds: RSS, YouTube channels, YouTube
        videos and YouTube playlists (imported as one source per video).

        A feed that fails is logged and counted as 0 so the others still run.
        """
        results = {}

        for feed_config in feeds_config:
            name = feed_config["name"]
            url = feed_config["url"]
            category = feed_config.get("category")

            logger.info("Processing feed: %s", name)

            try:
                if youtube.is_youtube_url(url):
                    count = self._fetch_youtube_feed(name, url, category)
                else:
                    self.add_feed(name, url, category)
                    count = self.process_feed(url)
            except (youtube.YouTubeError, ValueError) as e:
                logger.error("Feed %s failed: %s", name, e)
                count = 0
            results[name] = count

            logger.info("Downloaded %d new episodes from %s", count, name)

        return results

    # ------------------------------------------------------------------
    # YouTube
    # ------------------------------------------------------------------

    def _fetch_youtube_feed(
        self, name: str, url: str, category: Optional[str] = None
    ) -> int:
        """Fetch a YouTube URL from the config file. The configured name is
        used for a channel; videos keep their own titles."""
        ref = youtube.parse_youtube_url(url)
        if ref.kind == youtube.KIND_PLAYLIST:
            return self.import_youtube_playlist(ref.url, category)["downloaded"]

        if ref.kind == youtube.KIND_VIDEO:
            canonical = ref.url
            if not self.db.get_podcast_by_url(canonical):
                if self.db.episode_exists(canonical):
                    logger.info("Video already in the library: %s", canonical)
                    return 0
                self.add_feed(name, canonical, category, youtube.SOURCE_VIDEO)
        else:
            # Handles and /c/ names are looked up on each run; /channel/UC...
            # URLs are already canonical.
            if ref.url == youtube.channel_url(ref.id):
                canonical = ref.url
            else:
                canonical, _ = youtube.resolve_channel(ref.url)
            self.add_feed(name, canonical, category, youtube.SOURCE_CHANNEL)
        return self.process_feed(canonical)

    def _youtube_listing(self, url: str, limit: int) -> List[Dict]:
        ref = youtube.parse_youtube_url(url)
        if ref.kind == youtube.KIND_VIDEO:
            entries: List[Dict] = [{"id": ref.id, "title": ref.url}]
        elif ref.kind == youtube.KIND_CHANNEL:
            entries = youtube.list_channel_videos(ref.url, limit)
        else:
            _, entries, _ = youtube.list_playlist_videos(ref.url)
        return [
            {
                "title": e.get("title") or youtube.video_url(e["id"]),
                "url": youtube.video_url(e["id"]),
                "date": None,
                "description": e.get("description") or "",
                "guid": e["id"],
            }
            for e in entries
        ]

    def _process_youtube_source(
        self, podcast: Dict, episode_guids: Optional[List[str]] = None
    ) -> int:
        """Download new videos of a YouTube channel or single-video source.

        If ``episode_guids`` is given (video ids hand-picked from a preview
        listing), only those are downloaded instead of the usual top-N.

        Raises ``youtube.YouTubeError`` if the listing fails, or if every
        video attempted failed to download (so a blocked or broken yt-dlp
        shows up as a failed job instead of "0 new episodes").
        """
        ref = youtube.parse_youtube_url(podcast["rss_url"])
        if episode_guids is not None:
            videos = [(youtube.video_url(vid), vid) for vid in episode_guids]
        elif ref.kind == youtube.KIND_CHANNEL:
            entries = youtube.list_channel_videos(ref.url, self.max_episodes)
            videos = [
                (youtube.video_url(e["id"]), e.get("title") or e["id"]) for e in entries
            ]
        elif ref.kind == youtube.KIND_VIDEO:
            videos = [(ref.url, podcast["title"])]
        else:
            raise ValueError(f"Not a channel or video source: {podcast['rss_url']}")

        total = len(videos)
        downloaded = 0
        errors: List[str] = []
        for i, (video, label) in enumerate(videos):
            if self.db.episode_exists(video):
                continue
            if self.progress_callback:
                self.progress_callback(i, total, f"Downloading: {label}")
            try:
                title = self._ingest_youtube_video(podcast["id"], video)
            except youtube.YouTubeError as e:
                logger.warning("Failed to download %s: %s", video, e)
                errors.append(f"{video}: {e}")
                if self.progress_callback:
                    self.progress_callback(i + 1, total, f"Failed: {label}")
                continue
            if title is not None:
                downloaded += 1
            if self.progress_callback:
                done = f"Downloaded: {title}" if title else f"Skipped: {label}"
                self.progress_callback(i + 1, total, done)

        if errors and downloaded == 0:
            raise youtube.YouTubeError(
                f"{len(errors)} download(s) failed; first: {errors[0]}"
            )
        return downloaded

    def _ingest_youtube_video(
        self, podcast_id: int, video: str, info: Optional[Dict] = None
    ) -> Optional[str]:
        """Download one video's audio and add it as an episode.

        Returns the episode title, or None if the video is a Short,
        livestream or otherwise not downloadable. Raises
        ``youtube.YouTubeError`` on lookup or download failure.
        """
        if info is None:
            info = youtube.get_video_info(video)
        reason = youtube.video_rejection(info)
        if reason:
            logger.info("Skipping %s: %s", video, reason)
            return None

        title = info.get("title") or video
        url_hash = hashlib.sha1(video.encode()).hexdigest()[:10]
        # Same naming scheme as RSS episodes. Matched by id and hash only, so
        # a video renamed since an interrupted run still reuses its audio.
        existing = [
            p
            for p in self.audio_dir.glob(
                f"{podcast_id}_*_{url_hash}.{self.audio_format}"
            )
            if p.stat().st_size > 0
        ]
        if existing:
            logger.info("Reusing existing audio file for: %s", title)
            file_path: Optional[str] = str(existing[0])
        else:
            logger.info("Downloading: %s", title)
            filename = f"{podcast_id}_{_safe_filename(title)}_{url_hash}"
            with tempfile.TemporaryDirectory(prefix="p3-yt-") as tmpdir:
                raw = youtube.download_audio(info, tmpdir)
                file_path = self._normalize(raw, filename)
            if not file_path:
                raise youtube.YouTubeError(f"ffmpeg could not convert {video}")

        self.db.add_episode(
            podcast_id=podcast_id,
            title=title,
            date=youtube.upload_datetime(info),
            url=video,
            file_path=file_path,
        )
        logger.info("Downloaded: %s", title)
        return title

    def import_youtube_playlist(
        self, playlist_url: str, category: Optional[str] = None
    ) -> Dict[str, int]:
        """Import every video of a playlist as its own standalone source and
        download it. Re-importing a playlist is safe: videos already in the
        library are skipped and failed downloads are retried.

        Returns counts: downloaded, existing, skipped (Shorts, livestreams)
        and failed. Raises ``youtube.YouTubeError`` if the playlist cannot be
        read, or if every attempted video failed.
        """
        _, entries, dropped = youtube.list_playlist_videos(playlist_url)
        stats = {"downloaded": 0, "existing": 0, "skipped": dropped, "failed": 0}
        errors: List[str] = []
        total = len(entries)

        for i, entry in enumerate(entries):
            video = youtube.video_url(entry["id"])
            if self.db.episode_exists(video):
                stats["existing"] += 1
                continue
            if self.progress_callback:
                label = entry.get("title") or video
                self.progress_callback(i, total, f"Downloading: {label}")
            try:
                info = youtube.get_video_info(video)
                if youtube.video_rejection(info):
                    stats["skipped"] += 1
                    continue
                source = self.db.get_podcast_by_url(video)
                podcast_id = (
                    source["id"]
                    if source
                    else self.db.add_podcast(
                        info.get("title") or video,
                        video,
                        category,
                        youtube.SOURCE_VIDEO,
                    )
                )
                self._ingest_youtube_video(podcast_id, video, info)
                stats["downloaded"] += 1
            except youtube.YouTubeError as e:
                logger.warning("Failed to import %s: %s", video, e)
                errors.append(f"{video}: {e}")
                stats["failed"] += 1

        if self.progress_callback:
            self.progress_callback(total, total, "Playlist imported")
        if errors and stats["downloaded"] == 0 and stats["existing"] == 0:
            raise youtube.YouTubeError(
                f"{len(errors)} download(s) failed; first: {errors[0]}"
            )
        return stats
