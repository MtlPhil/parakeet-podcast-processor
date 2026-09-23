"""Tests for YouTube sources: URL parsing, filtering, listing, download,
resolution and the downloader's YouTube paths.

yt-dlp network access is replaced by stubbing ``youtube._extract``; the one
download test runs real yt-dlp against a local HTTP server.
"""

import http.server
import threading
from datetime import datetime, timezone

import pytest

from p3 import youtube
from p3.database import P3Database

CHANNEL_ID = "UC" + "a" * 22
VID_A = "aaaaaaaaaaa"
VID_B = "bbbbbbbbbbb"
VID_C = "ccccccccccc"


def _flat(vid, **extra):
    return {
        "_type": "url",
        "id": vid,
        "url": youtube.video_url(vid),
        "title": f"Video {vid}",
        **extra,
    }


def _info(vid, **extra):
    """Full metadata for a regular, public, already-aired upload."""
    return {
        "id": vid,
        "title": f"Video {vid}",
        "timestamp": 1700000000,
        "live_status": "not_live",
        "media_type": "video",
        "availability": "public",
        **extra,
    }


# ----------------------------------------------------------------------
# URL parsing
# ----------------------------------------------------------------------


class TestParseYoutubeUrl:
    @pytest.mark.parametrize(
        "url",
        [
            f"https://www.youtube.com/watch?v={VID_A}",
            f"https://youtube.com/watch?v={VID_A}&t=42s",
            f"https://m.youtube.com/watch?v={VID_A}",
            f"https://music.youtube.com/watch?v={VID_A}",
            f"https://youtu.be/{VID_A}",
            f"https://youtu.be/{VID_A}?si=tracking",
            f"https://www.youtube.com/live/{VID_A}",
            f"https://www.youtube.com/embed/{VID_A}",
            f"www.youtube.com/watch?v={VID_A}",
            # A video opened from a playlist is the single video.
            f"https://www.youtube.com/watch?v={VID_A}&list=PL123&index=3",
        ],
    )
    def test_video(self, url):
        ref = youtube.parse_youtube_url(url)
        assert ref.kind == youtube.KIND_VIDEO
        assert ref.id == VID_A
        assert ref.url == f"https://www.youtube.com/watch?v={VID_A}"

    def test_playlist(self):
        ref = youtube.parse_youtube_url(
            "https://www.youtube.com/playlist?list=PLabc_-123"
        )
        assert ref.kind == youtube.KIND_PLAYLIST
        assert ref.url == "https://www.youtube.com/playlist?list=PLabc_-123"

    @pytest.mark.parametrize(
        "url,expected",
        [
            (
                "https://www.youtube.com/@lexfridman",
                "https://www.youtube.com/@lexfridman",
            ),
            (
                "https://www.youtube.com/@lexfridman/videos",
                "https://www.youtube.com/@lexfridman",
            ),
            (
                f"https://www.youtube.com/channel/{CHANNEL_ID}/streams",
                f"https://www.youtube.com/channel/{CHANNEL_ID}",
            ),
            (
                "https://www.youtube.com/c/SomeName",
                "https://www.youtube.com/c/SomeName",
            ),
            (
                "https://www.youtube.com/user/oldname",
                "https://www.youtube.com/user/oldname",
            ),
            (
                "https://www.youtube.com/@caf%C3%A9",
                "https://www.youtube.com/@café",
            ),
        ],
    )
    def test_channel(self, url, expected):
        ref = youtube.parse_youtube_url(url)
        assert ref.kind == youtube.KIND_CHANNEL
        assert ref.url == expected

    def test_youtube_rss_feed_urls(self):
        ref = youtube.parse_youtube_url(
            f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
        )
        assert (ref.kind, ref.url) == (
            youtube.KIND_CHANNEL,
            f"https://www.youtube.com/channel/{CHANNEL_ID}",
        )
        ref = youtube.parse_youtube_url(
            "https://www.youtube.com/feeds/videos.xml?playlist_id=PL1"
        )
        assert ref.kind == youtube.KIND_PLAYLIST

    def test_surrounding_whitespace(self):
        url = f"  https://youtu.be/{VID_A}\n"
        assert youtube.is_youtube_url(url)
        assert youtube.parse_youtube_url(url).id == VID_A

    def test_shorts_rejected(self):
        with pytest.raises(ValueError, match="Shorts"):
            youtube.parse_youtube_url(f"https://www.youtube.com/shorts/{VID_A}")

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/",
            "https://www.youtube.com/watch?v=short",
            "https://www.youtube.com/feed/subscriptions",
            "https://www.youtube.com/results?search_query=x",
            "https://youtu.be/",
            "https://www.youtube.com/playlist",
        ],
    )
    def test_unsupported(self, url):
        with pytest.raises(ValueError):
            youtube.parse_youtube_url(url)

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://www.youtube.com/@x", True),
            ("https://youtu.be/abc", True),
            ("https://music.youtube.com/watch?v=abc", True),
            ("https://example.com/feed.xml", False),
            ("https://notyoutube.com/watch?v=abc", False),
            ("https://podcasts.apple.com/us/podcast/id123", False),
        ],
    )
    def test_is_youtube_url(self, url, expected):
        assert youtube.is_youtube_url(url) is expected


# ----------------------------------------------------------------------
# Filtering and dates
# ----------------------------------------------------------------------


class TestFiltering:
    def test_flat_keeps_regular_video(self):
        assert youtube.flat_entry_rejection(_flat(VID_A)) is None

    def test_flat_rejects_short_url(self):
        entry = _flat(VID_A, url=f"https://www.youtube.com/shorts/{VID_A}")
        assert youtube.flat_entry_rejection(entry) == "YouTube Short"

    @pytest.mark.parametrize("status", ["is_live", "is_upcoming", "was_live"])
    def test_flat_rejects_streams(self, status):
        assert youtube.flat_entry_rejection(_flat(VID_A, live_status=status))

    def test_flat_rejects_members_only_and_deleted(self):
        assert youtube.flat_entry_rejection(
            _flat(VID_A, availability="subscriber_only")
        )
        assert youtube.flat_entry_rejection(_flat(VID_A, title="[Deleted video]"))

    def test_full_keeps_regular_video_and_aired_premiere(self):
        assert youtube.video_rejection(_info(VID_A)) is None
        # yt-dlp reports an aired premiere as not_live / video.
        assert youtube.video_rejection(_info(VID_A, release_timestamp=1)) is None

    def test_full_rejects_short(self):
        assert youtube.video_rejection(_info(VID_A, media_type="short"))

    @pytest.mark.parametrize(
        "extra",
        [
            {"media_type": "livestream", "live_status": "was_live"},
            {"live_status": "is_live"},
            {"live_status": "is_upcoming"},
            {"live_status": "post_live"},
        ],
    )
    def test_full_rejects_streams(self, extra):
        assert youtube.video_rejection(_info(VID_A, **extra)) == "livestream"

    def test_upload_datetime(self):
        assert youtube.upload_datetime({"timestamp": 0}) == datetime(
            1970, 1, 1, tzinfo=timezone.utc
        )
        assert youtube.upload_datetime({"upload_date": "20240131"}) == datetime(
            2024, 1, 31, tzinfo=timezone.utc
        )
        assert youtube.upload_datetime({"upload_date": "bad"}) is None
        assert youtube.upload_datetime({}) is None


# ----------------------------------------------------------------------
# yt-dlp calls (stubbed)
# ----------------------------------------------------------------------


class FakeExtract:
    """Stands in for youtube._extract; records calls, serves canned info."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url, extra=None):
        self.calls.append((url, extra or {}))
        result = self.responses[url]
        if isinstance(result, Exception):
            raise result
        return result


class TestListing:
    def test_resolve_channel_uses_channel_id(self, monkeypatch):
        fake = FakeExtract(
            {
                "https://www.youtube.com/@pod/videos": {
                    "id": CHANNEL_ID,
                    "channel_id": CHANNEL_ID,
                    "channel": "The Pod",
                    "title": "The Pod - Videos",
                }
            }
        )
        monkeypatch.setattr(youtube, "_extract", fake)
        assert youtube.resolve_channel("https://www.youtube.com/@pod/featured") == (
            f"https://www.youtube.com/channel/{CHANNEL_ID}",
            "The Pod",
        )
        assert fake.calls[0][1]["extract_flat"] == "in_playlist"

    def test_resolve_channel_without_id_fails(self, monkeypatch):
        monkeypatch.setattr(
            youtube,
            "_extract",
            FakeExtract({"https://www.youtube.com/@pod/videos": {"id": "x"}}),
        )
        with pytest.raises(youtube.YouTubeError):
            youtube.resolve_channel("https://www.youtube.com/@pod")

    def test_list_channel_videos_filters_and_limits(self, monkeypatch):
        channel = f"https://www.youtube.com/channel/{CHANNEL_ID}"
        entries = [
            _flat(VID_A, live_status="is_upcoming"),
            _flat(VID_B),
            {"_type": "url", "id": "PLnested", "url": "x"},  # not a video
            _flat(VID_C),
            _flat("ddddddddddd"),
        ]
        fake = FakeExtract({f"{channel}/videos": {"entries": entries}})
        monkeypatch.setattr(youtube, "_extract", fake)

        got = youtube.list_channel_videos(channel, limit=2)

        assert [e["id"] for e in got] == [VID_B, VID_C]
        # Scans past the limit so filtered entries do not starve the fetch.
        assert fake.calls[0][1]["playlist_items"] == "1:12"

    def test_list_playlist_videos_dedupes_and_filters(self, monkeypatch):
        url = "https://www.youtube.com/playlist?list=PL1"
        entries = [
            _flat(VID_A),
            _flat(VID_A),
            _flat(VID_B, url=f"https://www.youtube.com/shorts/{VID_B}"),
            _flat(VID_C, title="[Private video]"),
        ]
        monkeypatch.setattr(
            youtube,
            "_extract",
            FakeExtract({url: {"title": "My list", "entries": entries}}),
        )
        title, got, dropped = youtube.list_playlist_videos(url)
        assert title == "My list"
        assert [e["id"] for e in got] == [VID_A]
        assert dropped == 2

    def test_wrong_kind_rejected(self):
        with pytest.raises(ValueError):
            youtube.list_channel_videos(youtube.video_url(VID_A), 3)
        with pytest.raises(ValueError):
            youtube.get_video_info("https://www.youtube.com/@pod")


class TestExtractErrors:
    def test_yt_dlp_error_becomes_youtube_error(self, monkeypatch):
        from yt_dlp.utils import DownloadError

        class Boom:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download):
                raise DownloadError("\x1b[0;31mERROR:\x1b[0m Sign in to confirm")

        monkeypatch.setattr(youtube, "_ydl", lambda extra=None: Boom())
        with pytest.raises(youtube.YouTubeError, match="Sign in to confirm"):
            youtube._extract("https://www.youtube.com/@x/videos")


# ----------------------------------------------------------------------
# Real yt-dlp download from already-extracted metadata
# ----------------------------------------------------------------------


@pytest.fixture
def audio_server():
    payload = b"\x00fake-audio" * 1000

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "audio/mp4")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", payload
    server.shutdown()


class TestDownloadAudio:
    def test_downloads_best_audio_format(self, tmp_path, audio_server):
        base, payload = audio_server
        info = {
            "id": VID_A,
            "title": "Episode",
            "extractor": "youtube",
            "extractor_key": "Youtube",
            "webpage_url": youtube.video_url(VID_A),
            "formats": [
                {
                    "format_id": "low",
                    "url": f"{base}/low.m4a",
                    "ext": "m4a",
                    "acodec": "mp4a.40.5",
                    "vcodec": "none",
                    "abr": 48,
                },
                {
                    "format_id": "high",
                    "url": f"{base}/high.m4a",
                    "ext": "m4a",
                    "acodec": "mp4a.40.2",
                    "vcodec": "none",
                    "abr": 128,
                },
            ],
        }

        path = youtube.download_audio(info, str(tmp_path))

        assert path == str(tmp_path / f"{VID_A}.m4a")
        assert (tmp_path / f"{VID_A}.m4a").read_bytes() == payload


class TestDownloadFallback:
    def test_stale_metadata_retried_from_page(self, tmp_path, monkeypatch):
        from yt_dlp.utils import DownloadError

        calls = []

        class FakeYDL:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            @staticmethod
            def sanitize_info(info, remove_private_keys=False):
                return info

            def process_ie_result(self, info, download):
                calls.append("info")
                raise DownloadError("HTTP Error 403: Forbidden")

            def download(self, urls):
                calls.append(urls)
                (tmp_path / f"{VID_A}.webm").write_bytes(b"audio")

        monkeypatch.setattr(youtube, "_ydl", lambda extra=None: FakeYDL())
        info = {"id": VID_A, "webpage_url": youtube.video_url(VID_A)}

        path = youtube.download_audio(info, str(tmp_path))

        assert calls == ["info", [youtube.video_url(VID_A)]]
        assert path == str(tmp_path / f"{VID_A}.webm")

    def test_no_file_is_an_error(self, tmp_path, monkeypatch):
        class FakeYDL:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            @staticmethod
            def sanitize_info(info, remove_private_keys=False):
                return info

            def process_ie_result(self, info, download):
                (tmp_path / f"{VID_A}.m4a.part").write_bytes(b"partial")

        monkeypatch.setattr(youtube, "_ydl", lambda extra=None: FakeYDL())
        with pytest.raises(youtube.YouTubeError, match="no file"):
            youtube.download_audio({"id": VID_A}, str(tmp_path))


# ----------------------------------------------------------------------
# Source resolution
# ----------------------------------------------------------------------


class TestResolveSource:
    def test_rss_unchanged(self):
        from p3.url_resolver import resolve_source

        src = resolve_source("https://example.com/feed.xml")
        assert (src.url, src.name, src.source_type) == (
            "https://example.com/feed.xml",
            None,
            "rss",
        )

    def test_channel(self, monkeypatch):
        from p3.url_resolver import resolve_source

        monkeypatch.setattr(
            youtube,
            "resolve_channel",
            lambda url: (youtube.channel_url(CHANNEL_ID), "The Pod"),
        )
        src = resolve_source("https://www.youtube.com/@pod")
        assert src.source_type == "youtube_channel"
        assert src.url == youtube.channel_url(CHANNEL_ID)

    def test_video(self, monkeypatch):
        from p3.url_resolver import resolve_source

        monkeypatch.setattr(youtube, "get_video_info", lambda url: _info(VID_A))
        src = resolve_source(f"https://youtu.be/{VID_A}")
        assert src.source_type == "youtube_video"
        assert src.url == youtube.video_url(VID_A)
        assert src.name == f"Video {VID_A}"

    def test_livestream_video_refused(self, monkeypatch):
        from p3.url_resolver import resolve_source

        monkeypatch.setattr(
            youtube,
            "get_video_info",
            lambda url: _info(VID_A, media_type="livestream", live_status="was_live"),
        )
        with pytest.raises(ValueError, match="livestream"):
            resolve_source(youtube.video_url(VID_A))

    def test_is_youtube_playlist(self):
        from p3.url_resolver import is_youtube_playlist

        assert is_youtube_playlist("https://www.youtube.com/playlist?list=PL1")
        assert not is_youtube_playlist(f"https://youtu.be/{VID_A}")
        assert not is_youtube_playlist("https://example.com/playlist?list=PL1")


# ----------------------------------------------------------------------
# Downloader YouTube paths (real database, stubbed yt-dlp and ffmpeg)
# ----------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    database = P3Database(str(tmp_path / "test.duckdb"))
    yield database
    database.close()


@pytest.fixture
def downloader(db, tmp_path, monkeypatch):
    from p3.downloader import PodcastDownloader

    dl = PodcastDownloader(db=db, data_dir=str(tmp_path / "data"), max_episodes=3)
    downloads = []

    def fake_download(info, outdir):
        downloads.append(info["id"])
        raw = f"{outdir}/{info['id']}.m4a"
        with open(raw, "wb") as f:
            f.write(b"raw")
        return raw

    def fake_normalize(input_path, filename):
        out = dl.audio_dir / f"{filename}.{dl.audio_format}"
        out.write_bytes(b"wav")
        return str(out)

    monkeypatch.setattr(youtube, "download_audio", fake_download)
    monkeypatch.setattr(dl, "_normalize", fake_normalize)
    dl.downloads = downloads
    return dl


def _stub_infos(monkeypatch, infos):
    def get_info(url):
        vid = youtube.parse_youtube_url(url).id
        result = infos[vid]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(youtube, "get_video_info", get_info)


class TestChannelFetch:
    def _channel(self, db):
        url = youtube.channel_url(CHANNEL_ID)
        db.add_podcast("Chan", url, None, youtube.SOURCE_CHANNEL)
        return url

    def test_downloads_new_videos_and_skips_shorts(self, db, downloader, monkeypatch):
        url = self._channel(db)
        monkeypatch.setattr(
            youtube,
            "list_channel_videos",
            lambda u, limit: [_flat(VID_A), _flat(VID_B), _flat(VID_C)],
        )
        _stub_infos(
            monkeypatch,
            {
                VID_A: _info(VID_A),
                VID_B: _info(VID_B, media_type="short"),
                VID_C: _info(VID_C, upload_date="20240101", timestamp=None),
            },
        )

        assert downloader.process_feed(url) == 2

        episodes = {e["url"]: e for e in db.get_all_episodes()}
        assert set(episodes) == {youtube.video_url(VID_A), youtube.video_url(VID_C)}
        assert downloader.downloads == [VID_A, VID_C]
        assert episodes[youtube.video_url(VID_A)]["title"] == f"Video {VID_A}"
        assert episodes[youtube.video_url(VID_C)]["date"] == datetime(2024, 1, 1)

        # Second fetch: nothing new, nothing re-downloaded.
        assert downloader.process_feed(url) == 0
        assert downloader.downloads == [VID_A, VID_C]

    def test_partial_failure_is_not_fatal(self, db, downloader, monkeypatch):
        url = self._channel(db)
        monkeypatch.setattr(
            youtube,
            "list_channel_videos",
            lambda u, limit: [_flat(VID_A), _flat(VID_B)],
        )
        _stub_infos(
            monkeypatch,
            {VID_A: youtube.YouTubeError("age restricted"), VID_B: _info(VID_B)},
        )
        assert downloader.process_feed(url) == 1

    def test_all_failures_raise(self, db, downloader, monkeypatch):
        url = self._channel(db)
        monkeypatch.setattr(
            youtube, "list_channel_videos", lambda u, limit: [_flat(VID_A)]
        )
        _stub_infos(monkeypatch, {VID_A: youtube.YouTubeError("Sign in to confirm")})
        with pytest.raises(youtube.YouTubeError, match="Sign in to confirm"):
            downloader.process_feed(url)

    def test_reuses_audio_from_interrupted_run(self, db, downloader, monkeypatch):
        import hashlib

        url = self._channel(db)
        pid = db.get_podcast_by_url(url)["id"]
        monkeypatch.setattr(
            youtube, "list_channel_videos", lambda u, limit: [_flat(VID_A)]
        )
        _stub_infos(monkeypatch, {VID_A: _info(VID_A, title="Renamed since")})
        url_hash = hashlib.sha1(youtube.video_url(VID_A).encode()).hexdigest()[:10]
        existing = downloader.audio_dir / f"{pid}_Old title_{url_hash}.wav"
        existing.write_bytes(b"done")

        assert downloader.process_feed(url) == 1
        assert downloader.downloads == []
        assert db.get_all_episodes()[0]["file_path"] == str(existing)


class TestVideoSource:
    def test_single_video_source(self, db, downloader, monkeypatch):
        url = youtube.video_url(VID_A)
        db.add_podcast("One video", url, None, youtube.SOURCE_VIDEO)
        _stub_infos(monkeypatch, {VID_A: _info(VID_A)})

        assert downloader.process_feed(url) == 1
        assert downloader.process_feed(url) == 0
        assert len(db.get_all_episodes()) == 1


class TestPlaylistImport:
    URL = "https://www.youtube.com/playlist?list=PL1"

    def test_each_video_becomes_its_own_source(self, db, downloader, monkeypatch):
        monkeypatch.setattr(
            youtube,
            "list_playlist_videos",
            lambda u: ("List", [_flat(VID_A), _flat(VID_B), _flat(VID_C)], 0),
        )
        _stub_infos(
            monkeypatch,
            {
                VID_A: _info(VID_A),
                VID_B: _info(VID_B, media_type="livestream", live_status="was_live"),
                VID_C: _info(VID_C),
            },
        )

        stats = downloader.import_youtube_playlist(self.URL, category="talks")

        assert stats == {"downloaded": 2, "existing": 0, "skipped": 1, "failed": 0}
        sources = {p["rss_url"]: p for p in db.get_all_podcasts()}
        assert set(sources) == {youtube.video_url(VID_A), youtube.video_url(VID_C)}
        for p in sources.values():
            assert p["source_type"] == "youtube_video"
            assert p["category"] == "talks"
            assert len(db.get_episodes_by_podcast(p["id"])) == 1

    def test_reimport_skips_existing_and_retries_failures(
        self, db, downloader, monkeypatch
    ):
        monkeypatch.setattr(
            youtube,
            "list_playlist_videos",
            lambda u: ("List", [_flat(VID_A), _flat(VID_B)], 0),
        )
        _stub_infos(monkeypatch, {VID_A: _info(VID_A), VID_B: _info(VID_B)})
        real_ingest = downloader._ingest_youtube_video
        calls = {"n": 0}

        def flaky(pid, video, info=None):
            calls["n"] += 1
            if video == youtube.video_url(VID_B) and calls["n"] <= 2:
                raise youtube.YouTubeError("network")
            return real_ingest(pid, video, info)

        monkeypatch.setattr(downloader, "_ingest_youtube_video", flaky)

        first = downloader.import_youtube_playlist(self.URL)
        assert first == {"downloaded": 1, "existing": 0, "skipped": 0, "failed": 1}
        # The failed video's source exists but has no episode yet.
        assert len(db.get_all_podcasts()) == 2

        second = downloader.import_youtube_playlist(self.URL)
        assert second == {"downloaded": 1, "existing": 1, "skipped": 0, "failed": 0}
        assert len(db.get_all_podcasts()) == 2
        assert len(db.get_all_episodes()) == 2

    def test_video_already_in_a_channel_is_not_duplicated(
        self, db, downloader, monkeypatch
    ):
        pid = db.add_podcast(
            "Chan", youtube.channel_url(CHANNEL_ID), None, youtube.SOURCE_CHANNEL
        )
        db.add_episode(pid, "A", None, youtube.video_url(VID_A), "/x.wav")
        monkeypatch.setattr(
            youtube, "list_playlist_videos", lambda u: ("List", [_flat(VID_A)], 0)
        )
        _stub_infos(monkeypatch, {})

        stats = downloader.import_youtube_playlist(self.URL)

        assert stats["existing"] == 1
        assert len(db.get_all_podcasts()) == 1

    def test_listing_drops_count_as_skipped(self, db, downloader, monkeypatch):
        monkeypatch.setattr(youtube, "list_playlist_videos", lambda u: ("List", [], 3))
        stats = downloader.import_youtube_playlist(self.URL)
        assert stats == {"downloaded": 0, "existing": 0, "skipped": 3, "failed": 0}

    def test_all_failures_raise(self, db, downloader, monkeypatch):
        monkeypatch.setattr(
            youtube, "list_playlist_videos", lambda u: ("List", [_flat(VID_A)], 0)
        )
        _stub_infos(monkeypatch, {VID_A: youtube.YouTubeError("blocked")})
        with pytest.raises(youtube.YouTubeError, match="blocked"):
            downloader.import_youtube_playlist(self.URL)


class TestConfigFeeds:
    def test_handle_is_resolved_to_canonical_channel(self, db, downloader, monkeypatch):
        monkeypatch.setattr(
            youtube,
            "resolve_channel",
            lambda u: (youtube.channel_url(CHANNEL_ID), "Resolved"),
        )
        monkeypatch.setattr(youtube, "list_channel_videos", lambda u, limit: [])

        results = downloader.fetch_all_feeds(
            [{"name": "My Chan", "url": "https://www.youtube.com/@pod"}]
        )

        assert results == {"My Chan": 0}
        [source] = db.get_all_podcasts()
        assert source["rss_url"] == youtube.channel_url(CHANNEL_ID)
        assert source["title"] == "My Chan"
        assert source["source_type"] == "youtube_channel"

    def test_failing_feed_does_not_stop_others(self, db, downloader, monkeypatch):
        def boom(u):
            raise youtube.YouTubeError("nope")

        monkeypatch.setattr(youtube, "resolve_channel", boom)
        monkeypatch.setattr(downloader, "process_feed", lambda url: 7)

        results = downloader.fetch_all_feeds(
            [
                {"name": "Bad", "url": "https://www.youtube.com/@bad"},
                {"name": "Good", "url": "https://example.com/feed.xml"},
            ]
        )
        assert results == {"Bad": 0, "Good": 7}

    def test_video_already_in_channel_is_not_added(self, db, downloader, monkeypatch):
        pid = db.add_podcast(
            "Chan", youtube.channel_url(CHANNEL_ID), None, youtube.SOURCE_CHANNEL
        )
        db.add_episode(pid, "A", None, youtube.video_url(VID_A), "/x.wav")

        results = downloader.fetch_all_feeds(
            [{"name": "Vid", "url": f"https://youtu.be/{VID_A}"}]
        )
        assert results == {"Vid": 0}
        assert len(db.get_all_podcasts()) == 1

    def test_dry_run_listing(self, downloader, monkeypatch):
        monkeypatch.setattr(
            youtube, "list_channel_videos", lambda u, limit: [_flat(VID_A)]
        )
        [ep] = downloader.fetch_episodes("https://www.youtube.com/@pod")
        assert ep["url"] == youtube.video_url(VID_A)
        assert ep["title"] == f"Video {VID_A}"
        assert ep["date"] is None
