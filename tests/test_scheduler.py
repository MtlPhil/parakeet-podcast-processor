"""Tests for the per-user daily run scheduler (no jobs are executed)."""

from datetime import datetime

import pytest

import p3.api.deps as deps
from p3.api import job_queue, tasks
from p3.api.scheduler import parse_run_time, run_if_due
from p3.api.tasks import run_key


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Temp database and config; jobs are recorded instead of run."""
    deps.close_db()
    monkeypatch.setattr(deps, "_DB_PATH", str(tmp_path / "test.duckdb"))
    monkeypatch.setattr(deps, "_CONFIG_PATH", str(tmp_path / "feeds.yaml"))
    deps.save_config({"feeds": [], "settings": {}})

    enqueued = []
    monkeypatch.setattr(
        job_queue.job_runner, "enqueue", lambda fn, *a: enqueued.append((fn, a))
    )
    yield deps.get_db(), enqueued
    deps.close_db()


def _user(db, name, **settings):
    db.ensure_user(name)
    db.update_user_settings(name, settings)
    return name


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
    def test_defaults_to_4am_and_can_be_turned_off(self, env):
        db, enqueued = env
        _user(db, "ann")
        _user(db, "bob", daily_run_time="")
        assert run_if_due(datetime(2026, 10, 17, 3, 59)) == []
        assert run_if_due(datetime(2026, 10, 17, 4, 0)) == ["ann"]
        assert _names(enqueued) == ["daily_queue_fetches"]
        assert enqueued[0][1][1] == "ann"

    def test_shared_config_time_is_ignored(self, env):
        # The run time is personal: a value in the shared config changes
        # nobody's run.
        db, enqueued = env
        deps.save_config({"feeds": [], "settings": {"daily_run_time": "06:00"}})
        _user(db, "ann")
        assert run_if_due(datetime(2026, 10, 17, 5, 0)) == ["ann"]

    def test_each_user_at_their_own_time(self, env):
        db, enqueued = env
        _user(db, "ann", daily_run_time="06:00")
        _user(db, "bob", daily_run_time="18:00")
        assert run_if_due(datetime(2026, 10, 17, 12, 0)) == ["ann"]
        assert run_if_due(datetime(2026, 10, 17, 18, 0)) == ["bob"]

    def test_runs_once_per_day(self, env):
        db, enqueued = env
        _user(db, "ann", daily_run_time="06:00")
        assert run_if_due(datetime(2026, 10, 17, 6, 0)) == ["ann"]
        assert run_if_due(datetime(2026, 10, 17, 18, 0)) == []
        assert run_if_due(datetime(2026, 10, 18, 5, 0)) == []
        assert run_if_due(datetime(2026, 10, 18, 6, 1)) == ["ann"]
        assert db.get_state(run_key("last_date", "ann")) == "2026-10-18"

    def test_last_run_survives_a_restart(self, env):
        db, enqueued = env
        _user(db, "ann", daily_run_time="06:00")
        assert run_if_due(datetime(2026, 10, 17, 6, 0)) == ["ann"]
        deps.close_db()
        assert run_if_due(datetime(2026, 10, 17, 9, 0)) == []


class TestDailyRunSteps:
    def test_owner_run_imports_new_config_feeds_first(self, env):
        db, enqueued = env
        owner = _user(db, "ann")
        feeds = [{"name": "New", "url": "http://example.com/new.rss"}]
        deps.save_config({"feeds": feeds, "settings": {}})

        run_id = tasks.queue_daily_run(db, owner)

        assert _names(enqueued) == ["task_import_feeds", "daily_queue_fetches"]
        assert enqueued[0][1][1] == feeds
        assert run_id in tasks.active_run_ids
        assert db.get_state(run_key("id", owner)) == run_id

    def test_other_users_never_import_config_feeds(self, env):
        db, enqueued = env
        _user(db, "ann")
        bob = _user(db, "bob")
        deps.save_config({"feeds": [{"name": "N", "url": "http://n"}], "settings": {}})
        tasks.queue_daily_run(db, bob)
        assert _names(enqueued) == ["daily_queue_fetches"]

    def test_fetch_step_covers_only_the_users_podcasts(self, env):
        db, enqueued = env
        ann = _user(db, "ann")
        mine = db.add_podcast("A", "http://example.com/a.rss")
        db.add_podcast("Theirs", "http://example.com/b.rss")
        db.subscribe(ann, mine)

        tasks.daily_queue_fetches("run-1", ann)

        assert _names(enqueued) == ["task_fetch", "daily_queue_pipelines"]
        assert enqueued[0][1][1:] == (mine, None, None, ann)
        (job,) = db.get_jobs_by_run("run-1")
        assert job["username"] == ann

    def test_pipeline_step_queues_the_users_unprocessed_episodes(self, env):
        db, enqueued = env
        ann = _user(db, "ann")
        pid = db.add_podcast("Pod", "http://example.com/rss")
        ids = {}
        for status in ("downloaded", "transcribed", "processed", "other_user"):
            eid = db.add_episode(
                pid, status, datetime(2026, 10, 1), f"http://example.com/{status}"
            )
            if status != "other_user":
                db.update_episode_status(eid, status)
                db.link_episode(ann, eid)
            ids[status] = eid

        tasks.daily_queue_pipelines("run-1", ann)

        assert _names(enqueued) == [
            "task_full_pipeline",
            "task_full_pipeline",
            "daily_queue_content",
        ]
        assert {a[1] for _, a in enqueued[:2]} == {
            ids["downloaded"],
            ids["transcribed"],
        }

    def test_content_step_is_opt_in_and_marks_the_run_finished(self, env):
        db, enqueued = env
        ann = _user(db, "ann")
        tasks.active_run_ids.add("run-1")

        tasks.daily_queue_content("run-1", ann)
        assert _names(enqueued) == ["daily_finish"]

        tasks.daily_finish("run-1", ann)
        assert db.get_state(run_key("finished_id", ann)) == "run-1"
        assert "run-1" not in tasks.active_run_ids

    def test_finish_emails_the_report_only_when_asked(self, env, monkeypatch):
        from p3 import mailer

        db, _ = env
        sent = []
        monkeypatch.setattr(mailer, "send_email", lambda *a: sent.append(a))
        ann = _user(db, "ann")
        tasks.daily_finish("run-1", ann)
        assert sent == []

        db.update_user_settings(ann, {"daily_run_email": True, "email": "a@b.c"})
        tasks.daily_finish("run-1", ann)
        assert [call[1] for call in sent] == ["a@b.c"]

    def test_a_mail_failure_is_recorded_and_does_not_raise(self, env, monkeypatch):
        from p3 import mailer

        db, _ = env

        def boom(*args):
            raise RuntimeError("smtp down")

        monkeypatch.setattr(mailer, "send_email", boom)
        ann = _user(db, "ann", daily_run_email=True, email="a@b.c")

        tasks.daily_finish("run-1", ann)

        assert db.get_state(run_key("finished_id", ann)) == "run-1"
        assert db.get_state(run_key("email_error", ann)) == "smtp down"

    def test_content_step_drafts_linkedin_for_completed_pipelines(self, env):
        db, enqueued = env
        ann = _user(db, "ann", daily_run_linkedin=True)
        pid = db.add_podcast("Pod", "http://example.com/rss")
        ok = db.add_episode(pid, "Ok", datetime(2026, 10, 1), "http://example.com/ok")
        bad = db.add_episode(pid, "Bad", datetime(2026, 10, 1), "http://example.com/b")
        for eid, status in ((ok, "completed"), (bad, "failed")):
            job = db.create_job(
                "full_pipeline", episode_id=eid, podcast_id=pid, run_id="run-1"
            )
            db.update_job(job, status=status)
        # A pipeline outside this run is ignored.
        other = db.create_job("full_pipeline", episode_id=bad, podcast_id=pid)
        db.update_job(other, status="completed")

        tasks.daily_queue_content("run-1", ann)

        assert _names(enqueued) == ["task_write_linkedin", "daily_finish"]
        assert enqueued[0][1][1:] == (ok, None, None, ann)

    def test_content_step_exports_only_the_users_digests(self, env):
        db, enqueued = env
        ann = _user(db, "ann", daily_run_export=True)
        deps.save_config({"feeds": [], "settings": {"export_format": ["html"]}})
        pid = db.add_podcast("Pod", "http://example.com/rss")
        eid = db.add_episode(pid, "Ep", datetime(2026, 10, 1), "http://example.com/1")
        db.add_summary(eid, [], [], [], [], "Summary")

        tasks.daily_queue_content("run-1", ann)
        assert _names(enqueued) == ["daily_finish"]  # not ann's episode

        enqueued.clear()
        db.link_episode(ann, eid)
        tasks.daily_queue_content("run-1", ann)

        assert _names(enqueued) == ["task_export", "daily_finish"]
        # HTML isn't produced by the export task, so the default formats apply.
        assert enqueued[0][1][2:] == (["markdown", "json"], ann)
