"""Tests for background task helpers in p3.api.tasks."""

import time
from unittest.mock import MagicMock

from p3.api.tasks import _start_heartbeat


def test_heartbeat_ticks_with_elapsed_message():
    db = MagicMock()
    stop = _start_heartbeat(db, "job1", "Working", interval=0.05)
    time.sleep(0.17)
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
    import p3.api.tasks as tasks

    monkeypatch.setattr(tasks, "get_db", lambda: db)
    monkeypatch.setattr(tasks, "_get_settings", lambda: {})


def test_import_playlist_reports_counts(monkeypatch):
    import p3.api.tasks as tasks
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

    tasks.task_import_playlist("job1", "https://www.youtube.com/playlist?list=PL1")

    final = db.update_job.call_args_list[-1]
    assert final.kwargs["status"] == "completed"
    assert final.kwargs["message"] == (
        "Imported 2 new videos (1 already in library, "
        "3 Shorts/livestreams skipped, 0 failed)"
    )


def test_import_playlist_failure_marks_job_failed(monkeypatch):
    import p3.api.tasks as tasks
    from p3 import youtube
    from p3.downloader import PodcastDownloader

    db = MagicMock()
    _stub_task_env(monkeypatch, db)

    def boom(self, url, category):
        raise youtube.YouTubeError("Sign in to confirm you're not a bot")

    monkeypatch.setattr(PodcastDownloader, "import_youtube_playlist", boom)

    tasks.task_import_playlist("job1", "https://www.youtube.com/playlist?list=PL1")

    final = db.update_job.call_args_list[-1]
    assert final.kwargs["status"] == "failed"
    assert "not a bot" in final.kwargs["error"]


def _bulk_env(monkeypatch, tmp_path):
    """Real temp DB; URL resolution faked, queued jobs recorded, not run."""
    import p3.api.tasks as tasks
    import p3.url_resolver as url_resolver
    from p3.database import P3Database
    from p3.url_resolver import ResolvedSource

    db = P3Database(str(tmp_path / "test.duckdb"))
    _stub_task_env(monkeypatch, db)
    enqueued = []
    monkeypatch.setattr(
        tasks.job_runner, "enqueue", lambda fn, *a: enqueued.append((fn.__name__, a))
    )

    def fake_resolve(url):
        if "bad" in url:
            raise ValueError("not a feed")
        return ResolvedSource(url=url, name=url.rsplit("/", 1)[-1], source_type="rss")

    monkeypatch.setattr(url_resolver, "resolve_source", fake_resolve)
    return db, enqueued


def test_bulk_add_adds_new_sources_and_reports_the_rest(monkeypatch, tmp_path):
    import p3.api.tasks as tasks

    db, enqueued = _bulk_env(monkeypatch, tmp_path)
    db.add_podcast("Old", "http://example.com/old")
    job = db.create_job("bulk_add")

    tasks.task_bulk_add(
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
    import p3.api.tasks as tasks

    db, enqueued = _bulk_env(monkeypatch, tmp_path)
    job = db.create_job("bulk_add")

    tasks.task_bulk_add(job, ["http://example.com/bad"])

    assert db.get_job(job)["status"] == "failed"
    assert enqueued == []
    db.close()
