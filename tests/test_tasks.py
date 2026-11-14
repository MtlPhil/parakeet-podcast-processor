"""Tests for the pipeline jobs in p3.api.tasks.pipeline."""

import time
from unittest.mock import MagicMock

import pytest

from p3.api.tasks.pipeline import _start_heartbeat


def test_heartbeat_ticks_with_elapsed_message():
    db = MagicMock()
    stop = _start_heartbeat(db, "job1", "Working", interval=0.05)
    # Wait for the ticks rather than a fixed time: on a busy CI runner the
    # heartbeat thread can be scheduled late.
    deadline = time.monotonic() + 5
    while db.update_job.call_count < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop()

    assert db.update_job.call_count >= 2
    args, kwargs = db.update_job.call_args_list[0]
    assert args == ("job1",)
    assert kwargs["message"].startswith("Working (")
    assert "elapsed" in kwargs["message"]


def test_heartbeat_stop_prevents_further_ticks():
    db = MagicMock()
    stop = _start_heartbeat(db, "job1", "Working", interval=0.05)
    time.sleep(0.12)
    stop()
    count_after_stop = db.update_job.call_count

    time.sleep(0.15)
    assert db.update_job.call_count == count_after_stop


def test_heartbeat_stop_before_any_tick_is_safe():
    db = MagicMock()
    stop = _start_heartbeat(db, "job1", "Working", interval=5.0)
    stop()
    db.update_job.assert_not_called()


def _stub_task_env(monkeypatch, db):
    from p3.api.tasks import pipeline

    monkeypatch.setattr(pipeline, "get_db", lambda: db)
    monkeypatch.setattr(pipeline, "_get_settings", lambda: {})


def test_import_playlist_reports_counts(monkeypatch):
    from p3.api.tasks import pipeline
    from p3.downloader import PodcastDownloader

    db = MagicMock()
    _stub_task_env(monkeypatch, db)
    monkeypatch.setattr(
        PodcastDownloader,
        "import_youtube_playlist",
        lambda self, url, category: {
            "downloaded": 2,
            "existing": 1,
            "skipped": 3,
            "failed": 0,
        },
    )

    pipeline.task_import_playlist("job1", "https://www.youtube.com/playlist?list=PL1")

    final = db.update_job.call_args_list[-1]
    assert final.kwargs["status"] == "completed"
    assert final.kwargs["message"] == (
        "Imported 2 new videos (1 already in library, "
        "3 Shorts/livestreams skipped, 0 failed)"
    )


def test_import_playlist_failure_marks_job_failed(monkeypatch):
    from p3 import youtube
    from p3.api.tasks import pipeline
    from p3.downloader import PodcastDownloader

    db = MagicMock()
    _stub_task_env(monkeypatch, db)

    def boom(self, url, category):
        raise youtube.YouTubeError("Sign in to confirm you're not a bot")

    monkeypatch.setattr(PodcastDownloader, "import_youtube_playlist", boom)

    pipeline.task_import_playlist("job1", "https://www.youtube.com/playlist?list=PL1")

    final = db.update_job.call_args_list[-1]
    assert final.kwargs["status"] == "failed"
    assert "not a bot" in final.kwargs["error"]


def _bulk_env(monkeypatch, tmp_path):
    """Real temp DB; URL resolution faked, queued jobs recorded, not run."""
    import p3.url_resolver as url_resolver
    from p3.api.tasks import pipeline
    from p3.database import P3Database
    from p3.url_resolver import ResolvedSource

    db = P3Database(str(tmp_path / "test.duckdb"))
    _stub_task_env(monkeypatch, db)
    enqueued = []
    monkeypatch.setattr(
        pipeline.job_runner, "enqueue", lambda fn, *a: enqueued.append((fn.__name__, a))
    )
    monkeypatch.setattr(
        pipeline.job_runner,
        "enqueue_priority",
        lambda fn, *a: enqueued.append((fn.__name__, a)),
    )

    def fake_resolve(url):
        if "bad" in url:
            raise ValueError("not a feed")
        return ResolvedSource(url=url, name=url.rsplit("/", 1)[-1], source_type="rss")

    monkeypatch.setattr(url_resolver, "resolve_source", fake_resolve)
    return db, enqueued


def test_bulk_add_adds_new_sources_and_reports_the_rest(monkeypatch, tmp_path):
    from p3.api.tasks import pipeline

    db, enqueued = _bulk_env(monkeypatch, tmp_path)
    db.add_podcast("Old", "http://example.com/old")
    job = db.create_job("bulk_add")

    pipeline.task_bulk_add(
        job,
        [
            "http://example.com/new",
            "http://example.com/old",
            "http://example.com/bad",
            "https://www.youtube.com/playlist?list=PL1234567890",
        ],
        "tech",
    )

    result = db.get_job(job)
    assert result["status"] == "completed"
    assert result["message"] == "Added 2, 1 already in library, 1 failed"
    assert "http://example.com/bad: not a feed" in result["error"]
    new = db.get_podcast_by_url("http://example.com/new")
    assert new["title"] == "new" and new["category"] == "tech"
    assert [name for name, _ in enqueued] == ["task_fetch", "task_import_playlist"]
    db.close()


def test_bulk_add_fails_when_every_url_fails(monkeypatch, tmp_path):
    from p3.api.tasks import pipeline

    db, enqueued = _bulk_env(monkeypatch, tmp_path)
    job = db.create_job("bulk_add")

    pipeline.task_bulk_add(job, ["http://example.com/bad"])

    assert db.get_job(job)["status"] == "failed"
    assert enqueued == []
    db.close()


class TestReleaseOllama:
    def _released(self, monkeypatch, settings):
        from p3.api.tasks import pipeline

        calls = []
        monkeypatch.setattr(pipeline, "release_ollama_models", calls.append)
        pipeline._release_ollama(settings)
        return calls

    def test_releases_by_default(self, monkeypatch):
        assert self._released(monkeypatch, {}) == ["http://localhost:11434"]

    def test_uses_configured_ollama_url(self, monkeypatch):
        settings = {"ollama_base_url": "http://mac:11434"}
        assert self._released(monkeypatch, settings) == ["http://mac:11434"]

    def test_admin_can_turn_it_off(self, monkeypatch):
        settings = {"release_ollama_before_transcription": False}
        assert self._released(monkeypatch, settings) == []


class TestRefreshDigests:
    def _env(self, monkeypatch, tmp_path, minutes_per_episode=0):
        import json
        from datetime import datetime, timedelta

        from p3.api.tasks import pipeline
        from p3.database import P3Database
        from p3.downloader import PodcastDownloader

        db = P3Database(str(tmp_path / "test.duckdb"))
        pid = db.add_podcast("Pod", "http://example.com/rss")
        ids = []
        for i in range(3):
            eid = db.add_episode(pid, f"E{i}", None, f"http://x/{i}.mp3")
            db.add_transcript_segments(
                eid,
                [
                    {
                        "start": 0,
                        "end": 1,
                        "text": "le podcast est dans la ville et nous",
                        "speaker": "",
                        "confidence": 1,
                    }
                ],
            )
            db.add_summary(eid, [], [], [], [], "s")
            db.update_episode_status(eid, "processed")
            ids.append(eid)
        db.set_state(pipeline.REFRESHED_KEY, json.dumps([ids[0]]))
        monkeypatch.setattr(pipeline, "get_db", lambda: db)
        monkeypatch.setattr(pipeline, "_get_settings", lambda: {})
        monkeypatch.setattr(PodcastDownloader, "fill_missing_show_notes", lambda s: 2)

        clock = {"now": datetime(2026, 11, 1, 1, 0)}

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock["now"]

        monkeypatch.setattr(pipeline, "datetime", FakeDatetime)
        refreshed = []

        class FakeCleaner:
            def refresh_digest(self, episode_id):
                refreshed.append(episode_id)
                clock["now"] += timedelta(minutes=minutes_per_episode)

        monkeypatch.setattr(pipeline, "_make_cleaner", lambda db, s: FakeCleaner())
        self.indexed = []
        monkeypatch.setattr(
            pipeline,
            "_index_episode",
            lambda db, episode_id, s: self.indexed.append(episode_id),
        )
        return pipeline, db, ids, refreshed

    def test_refreshes_what_is_left_and_detects_language(self, monkeypatch, tmp_path):
        pipeline, db, ids, refreshed = self._env(monkeypatch, tmp_path)
        try:
            job = db.create_job("refresh")
            pipeline.task_refresh_digests(job)
            assert sorted(refreshed) == ids[1:]
            # Ask must not keep citing the digest that was just replaced.
            assert self.indexed == refreshed
            assert db.get_job(job)["status"] == "completed"
            assert (
                "2 digest(s) refreshed, 0 failed, 0 left" in db.get_job(job)["message"]
            )
            assert db.get_episode_by_id(ids[1])["language"] == "fr"
            pipeline.task_refresh_digests(db.create_job("refresh"))
            assert len(refreshed) == 2  # nothing left to do
        finally:
            db.close()

    def test_stops_before_the_deadline(self, monkeypatch, tmp_path):
        # 01:00 now; 20 minutes an episode; stop at 01:30. The first starts
        # (15-minute default estimate), the second would end after 01:30.
        pipeline, db, ids, refreshed = self._env(
            monkeypatch, tmp_path, minutes_per_episode=20
        )
        try:
            job = db.create_job("refresh")
            pipeline.task_refresh_digests(job, "01:30")
            assert len(refreshed) == 1
            assert (
                "1 digest(s) refreshed, 0 failed, 1 left" in db.get_job(job)["message"]
            )
        finally:
            db.close()

    def test_deadline_is_the_next_such_time(self, monkeypatch):
        from datetime import datetime

        from p3.api.tasks import pipeline

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 31, 23, 0)

        monkeypatch.setattr(pipeline, "datetime", FakeDatetime)
        assert pipeline._refresh_deadline("03:40") == datetime(2026, 11, 1, 3, 40)
        assert pipeline._refresh_deadline("23:30") == datetime(2026, 10, 31, 23, 30)
        assert pipeline._refresh_deadline(None) is None


class TestPipeline:
    """task_transcribe, task_digest and task_full_pipeline with the speech
    engine and the LLM replaced by stand-ins, on a real temp database."""

    @pytest.fixture
    def env(self, monkeypatch, tmp_path):
        from p3.api.tasks import pipeline
        from p3.database import P3Database

        db = P3Database(str(tmp_path / "test.duckdb"))
        _stub_task_env(monkeypatch, db)
        monkeypatch.setattr(pipeline, "_release_ollama", lambda settings: None)
        pid = db.add_podcast("Pod", "http://example.com/rss")
        eid = db.add_episode(pid, "Ep", None, "http://example.com/ep.mp3")
        events = []

        class Transcriber:
            def transcribe_episode(self, episode_id):
                events.append("transcribe")
                db.store_transcript(episode_id, [{"text": "Hi", "start": 0}], "en")

            def unload_models(self):
                events.append("unload")

        class Cleaner:
            def generate_summary(self, episode_id):
                events.append("digest")
                db.update_episode_status(episode_id, "processed")

        monkeypatch.setattr(pipeline, "_make_transcriber", lambda db, s: Transcriber())
        monkeypatch.setattr(pipeline, "_make_cleaner", lambda db, s: Cleaner())
        yield pipeline, db, eid, events, Transcriber, Cleaner
        db.close()

    def _run(self, db, task, eid, job_type="full_pipeline"):
        job_id = db.create_job(job_type, episode_id=eid)
        task(job_id, eid)
        return db.get_job(job_id)

    def test_full_pipeline_transcribes_then_digests(self, env):
        pipeline, db, eid, events, *_ = env
        job = self._run(db, pipeline.task_full_pipeline, eid)
        assert job["status"] == "completed"
        assert events == ["transcribe", "unload", "digest"]
        assert db.get_episode_by_id(eid)["status"] == "processed"

    def test_transcribe_stops_before_the_digest(self, env):
        pipeline, db, eid, events, *_ = env
        job = self._run(db, pipeline.task_transcribe, eid, "transcribe")
        assert (job["status"], job["message"]) == ("completed", "Transcribed: Ep")
        assert events == ["transcribe", "unload"]

    def test_steps_already_done_are_skipped(self, env):
        pipeline, db, eid, events, *_ = env
        db.update_episode_status(eid, "transcribed")
        assert self._run(db, pipeline.task_full_pipeline, eid)["status"] == "completed"
        assert events == ["digest"]

    def test_a_failed_transcription_gives_its_reason_and_unloads_the_models(
        self, env, monkeypatch
    ):
        from p3.transcriber import TranscriptionError

        pipeline, db, eid, events, Transcriber, _ = env

        def fail(self, episode_id):
            raise TranscriptionError("Whisper failed: out of memory")

        monkeypatch.setattr(Transcriber, "transcribe_episode", fail)
        job = self._run(db, pipeline.task_full_pipeline, eid)
        assert (job["status"], job["error"]) == (
            "failed",
            "Whisper failed: out of memory",
        )
        assert events == ["unload"]  # no digest, and the memory is freed

    def test_a_failed_digest_gives_its_reason(self, env, monkeypatch):
        from p3.cleaner import DigestError

        pipeline, db, eid, events, _, Cleaner = env
        db.update_episode_status(eid, "transcribed")

        def fail(self, episode_id):
            raise DigestError("The summary failed: connection refused")

        monkeypatch.setattr(Cleaner, "generate_summary", fail)
        for task, kind in (
            (pipeline.task_digest, "digest"),
            (pipeline.task_full_pipeline, "full_pipeline"),
        ):
            job = self._run(db, task, eid, kind)
            assert job["status"] == "failed"
            assert job["error"] == "The summary failed: connection refused"

    def test_a_missing_episode_fails(self, env):
        pipeline, db, *_ = env
        job = self._run(db, pipeline.task_full_pipeline, 999)
        assert (job["status"], job["error"]) == ("failed", "Episode 999 not found")
