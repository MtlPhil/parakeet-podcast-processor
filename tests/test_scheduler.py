"""Tests for the daily run scheduler (no jobs are executed)."""

from datetime import datetime

import pytest

import p3.api.deps as deps
from p3.api import job_queue
from p3.api.scheduler import LAST_RUN_KEY, parse_run_time, run_if_due
from p3.api.tasks import queue_pending_pipelines


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Temp database and config; jobs are recorded instead of run."""
    deps.close_db()
    monkeypatch.setattr(deps, "_DB_PATH", str(tmp_path / "test.duckdb"))
    monkeypatch.setattr(deps, "_CONFIG_PATH", str(tmp_path / "feeds.yaml"))

    enqueued = []
    monkeypatch.setattr(
        job_queue.job_runner, "enqueue", lambda fn, *a: enqueued.append((fn, a))
    )

    def set_run_time(value):
        deps.save_config({"feeds": [], "settings": {"daily_run_time": value}})

    yield deps.get_db(), enqueued, set_run_time
    deps.close_db()


def _names(enqueued):
    return [fn.__name__ for fn, _ in enqueued]


class TestParseRunTime:
    def test_valid(self):
        assert parse_run_time("06:00") == (6, 0)
        assert parse_run_time("6:30") == (6, 30)
        assert parse_run_time(" 23:59 ") == (23, 59)

    def test_yaml_base60_integer(self):
        # An unquoted 6:30 in YAML loads as 6 * 60 + 30.
        assert parse_run_time(390) == (6, 30)

    def test_unset_or_invalid_disables(self):
        assert parse_run_time(None) is None
        assert parse_run_time("") is None
        assert parse_run_time("25:00") is None
        assert parse_run_time("morning") is None


class TestRunIfDue:
    def test_disabled_without_setting(self, env):
        db, enqueued, set_run_time = env
        set_run_time("")
        db.add_podcast("Pod", "http://example.com/rss")
        assert run_if_due(datetime(2026, 10, 17, 12, 0)) is False
        assert enqueued == []

    def test_not_due_before_run_time(self, env):
        db, enqueued, set_run_time = env
        set_run_time("06:00")
        db.add_podcast("Pod", "http://example.com/rss")
        assert run_if_due(datetime(2026, 10, 17, 5, 59)) is False
        assert enqueued == []

    def test_queues_fetch_per_source_then_pipelines(self, env):
        db, enqueued, set_run_time = env
        set_run_time("06:00")
        p1 = db.add_podcast("A", "http://example.com/a.rss")
        p2 = db.add_podcast("B", "http://example.com/b.rss")

        assert run_if_due(datetime(2026, 10, 17, 6, 0)) is True

        # One fetch job per source, then the follow-up that queues the
        # pipeline jobs -- last, so it runs after every fetch.
        assert _names(enqueued) == [
            "task_fetch",
            "task_fetch",
            "queue_pending_pipelines",
        ]
        assert {a[1] for _, a in enqueued[:2]} == {p1, p2}
        assert db.get_state(LAST_RUN_KEY) == "2026-10-17"

    def test_runs_once_per_day(self, env):
        db, enqueued, set_run_time = env
        set_run_time("06:00")
        db.add_podcast("Pod", "http://example.com/rss")

        assert run_if_due(datetime(2026, 10, 17, 6, 0)) is True
        assert run_if_due(datetime(2026, 10, 17, 18, 0)) is False
        assert run_if_due(datetime(2026, 10, 18, 5, 0)) is False
        assert run_if_due(datetime(2026, 10, 18, 6, 1)) is True
        assert _names(enqueued).count("queue_pending_pipelines") == 2

    def test_catches_up_late_in_the_day(self, env):
        # e.g. the Mac was asleep at 06:00 and woke at 14:00.
        db, enqueued, set_run_time = env
        set_run_time("06:00")
        assert run_if_due(datetime(2026, 10, 17, 14, 0)) is True

    def test_last_run_survives_a_restart(self, env):
        db, enqueued, set_run_time = env
        set_run_time("06:00")
        assert run_if_due(datetime(2026, 10, 17, 6, 0)) is True
        deps.close_db()
        assert run_if_due(datetime(2026, 10, 17, 9, 0)) is False


class TestQueuePendingPipelines:
    def test_queues_only_unprocessed_episodes(self, env):
        db, enqueued, _ = env
        pid = db.add_podcast("Pod", "http://example.com/rss")
        ids = {}
        for status in ("downloaded", "transcribed", "processed"):
            eid = db.add_episode(
                pid, status, datetime(2026, 10, 1), f"http://example.com/{status}"
            )
            db.update_episode_status(eid, status)
            ids[status] = eid

        queue_pending_pipelines()

        assert _names(enqueued) == ["task_full_pipeline", "task_full_pipeline"]
        queued_episodes = {a[1] for _, a in enqueued}
        assert queued_episodes == {ids["downloaded"], ids["transcribed"]}
