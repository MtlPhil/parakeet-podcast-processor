"""Tests for downloader utilities.

Most tests here exercise pure utility functions without importing the full
downloader module (which requires feedparser, which may not be installable
in all environments). TestProcessFeedResume does import it — network and
ffmpeg calls are mocked out, matching the "no mocking of external services
beyond that" convention: only PodcastDownloader's own methods are stubbed.
"""

import hashlib
import re
from unittest.mock import MagicMock


def _safe_filename(title: str, max_length: int = 50) -> str:
    """Mirror of p3.downloader._safe_filename for isolated testing."""
    cleaned = re.sub(r"[^\w\s-]", "", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        cleaned = "untitled"
    return cleaned[:max_length]


class TestSafeFilename:
    def test_basic_title(self):
        assert _safe_filename("My Podcast Episode") == "My Podcast Episode"

    def test_special_characters_stripped(self):
        result = _safe_filename("Episode: The Best! (Part 1)")
        assert ":" not in result
        assert "!" not in result
        assert "(" not in result

    def test_truncation(self):
        long_title = "A" * 100
        result = _safe_filename(long_title, max_length=50)
        assert len(result) == 50

    def test_empty_title(self):
        assert _safe_filename("") == "untitled"

    def test_all_special_chars(self):
        assert _safe_filename("!!!???...") == "untitled"

    def test_whitespace_collapse(self):
        result = _safe_filename("Too   many    spaces")
        assert "  " not in result


class TestProcessFeedResume:
    """A completed download+normalize must survive an interrupted process:
    process_feed uses a stable (non-timestamped) filename per episode and
    reuses it if already present, instead of redoing the download and the
    slow ffmpeg normalize."""

    def _episode(self, url="http://example.com/ep.mp3", title="Ep 1"):
        return {
            "title": title,
            "url": url,
            "date": None,
            "description": "",
            "guid": url,
        }

    def _downloader(self, tmp_path, progress_callback=None):
        from p3.downloader import PodcastDownloader

        db = MagicMock()
        db.get_podcast_by_url.return_value = {"id": 1, "title": "Pod"}
        db.episode_exists.return_value = False
        dl = PodcastDownloader(
            db=db,
            data_dir=str(tmp_path),
            max_episodes=5,
            progress_callback=progress_callback,
        )
        return dl, db

    def test_reuses_existing_file_without_redownloading(self, tmp_path, monkeypatch):
        ep = self._episode()
        dl, db = self._downloader(tmp_path)
        monkeypatch.setattr(dl, "fetch_episodes", lambda url: [ep])

        # Pre-create the deterministic output file, as if a prior run
        # finished the download+normalize but was interrupted before recording it.
        url_hash = hashlib.sha1(ep["url"].encode()).hexdigest()[:10]
        expected_path = tmp_path / "audio" / f"1_Ep 1_{url_hash}.wav"
        expected_path.parent.mkdir(parents=True, exist_ok=True)
        expected_path.write_bytes(b"fake audio")

        def boom(*a, **k):
            raise AssertionError("should not re-download when the file already exists")

        monkeypatch.setattr(dl, "download_episode", boom)

        count = dl.process_feed("http://example.com/feed.xml")

        assert count == 1
        db.add_episode.assert_called_once()
        assert db.add_episode.call_args.kwargs["file_path"] == str(expected_path)

    def test_downloads_when_no_existing_file(self, tmp_path, monkeypatch):
        ep = self._episode(url="http://example.com/ep2.mp3", title="Ep 2")
        dl, db = self._downloader(tmp_path)
        monkeypatch.setattr(dl, "fetch_episodes", lambda url: [ep])
        monkeypatch.setattr(
            dl, "download_episode", lambda url, filename: f"data/audio/{filename}.wav"
        )

        count = dl.process_feed("http://example.com/feed.xml")

        assert count == 1
        db.add_episode.assert_called_once()

    def test_progress_callback_reports_per_episode_messages(
        self, tmp_path, monkeypatch
    ):
        ep = self._episode(url="http://example.com/ep3.mp3", title="Ep 3")
        calls = []
        dl, db = self._downloader(
            tmp_path,
            progress_callback=lambda done, total, msg: calls.append((done, total, msg)),
        )
        monkeypatch.setattr(dl, "fetch_episodes", lambda url: [ep])
        monkeypatch.setattr(
            dl, "download_episode", lambda url, filename: f"data/audio/{filename}.wav"
        )

        dl.process_feed("http://example.com/feed.xml")

        assert calls == [
            (0, 1, "Downloading: Ep 3"),
            (1, 1, "Downloaded: Ep 3"),
        ]

    def test_skips_episodes_already_in_db(self, tmp_path, monkeypatch):
        ep = self._episode()
        dl, db = self._downloader(tmp_path)
        db.episode_exists.return_value = True
        monkeypatch.setattr(dl, "fetch_episodes", lambda url: [ep])

        count = dl.process_feed("http://example.com/feed.xml")

        assert count == 0
        db.add_episode.assert_not_called()


class TestFetchEpisodesDates:
    _FEED = """<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>A</title><pubDate>Tue, 02 Sep 2025 14:30:00 -0400</pubDate>
<enclosure url="http://x/a.mp3" type="audio/mpeg"/></item>
<item><title>B</title><enclosure url="http://x/b.mp3" type="audio/mpeg"/></item>
</channel></rss>"""

    def test_publication_dates_are_utc_or_none(self, tmp_path, monkeypatch):
        from datetime import datetime, timezone

        import feedparser

        from p3 import downloader

        feed = feedparser.parse(self._FEED)
        monkeypatch.setattr(downloader.feedparser, "parse", lambda url: feed)
        dl = downloader.PodcastDownloader(db=MagicMock(), data_dir=str(tmp_path))

        episodes = dl.fetch_episodes("http://example.com/rss")

        assert episodes[0]["date"] == datetime(2025, 9, 2, 18, 30, tzinfo=timezone.utc)
        assert episodes[1]["date"] is None
