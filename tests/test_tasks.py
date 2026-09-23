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
