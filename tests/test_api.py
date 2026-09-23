"""Tests for the FastAPI API endpoints."""

import os
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# Patch deps before importing the app so it uses a temp database
_tmp_dir = tempfile.mkdtemp()
_tmp_db = os.path.join(_tmp_dir, "test.duckdb")

import p3.api.deps as deps

deps._DB_PATH = _tmp_db
deps._CONFIG_PATH = os.path.join(_tmp_dir, "feeds.yaml")

# Write a minimal config
Path(deps._CONFIG_PATH).write_text(
    "feeds: []\nsettings:\n  llm_provider: ollama\n  llm_model: llama3.2:latest\n"
)

from p3.api.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_db():
    """Ensure a clean database for each test."""
    deps.close_db()
    if os.path.exists(_tmp_db):
        os.unlink(_tmp_db)
    yield
    deps.close_db()


# ------------------------------------------------------------------
# Stats
# ------------------------------------------------------------------


class TestStats:
    def test_get_stats(self):
        resp = client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_podcasts"] == 0
        assert data["total_episodes"] == 0

    def test_get_stats_splits_running_and_queued(self):
        db = deps.get_db()
        pid = db.add_podcast("Pod", "http://example.com/rss")
        db.create_job("fetch", podcast_id=pid)  # pending
        running = db.create_job("fetch", podcast_id=pid)
        db.update_job(running, status="running")

        data = client.get("/api/stats").json()
        assert data["queued_jobs"] == 1
        assert data["running_jobs"] == 1
        assert data["active_jobs"] == 2


# ------------------------------------------------------------------
# Podcasts
# ------------------------------------------------------------------


class TestPodcasts:
    def test_list_empty(self):
        resp = client.get("/api/podcasts")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_add_podcast(self):
        resp = client.post(
            "/api/podcasts",
            json={
                "url": "http://example.com/feed.xml",
                "name": "Test Podcast",
                "category": "tech",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "podcast_id" in data
        assert "job_id" in data

    def test_add_duplicate_podcast(self):
        client.post("/api/podcasts", json={"url": "http://example.com/feed.xml"})
        resp = client.post("/api/podcasts", json={"url": "http://example.com/feed.xml"})
        assert resp.status_code == 409

    def test_get_podcast(self):
        result = client.post(
            "/api/podcasts",
            json={
                "url": "http://example.com/feed.xml",
                "name": "My Pod",
            },
        ).json()
        resp = client.get(f"/api/podcasts/{result['podcast_id']}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "My Pod"

    def test_get_podcast_not_found(self):
        resp = client.get("/api/podcasts/999")
        assert resp.status_code == 404

    def test_delete_podcast(self):
        result = client.post(
            "/api/podcasts",
            json={
                "url": "http://example.com/feed.xml",
            },
        ).json()
        resp = client.delete(f"/api/podcasts/{result['podcast_id']}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

        resp = client.get(f"/api/podcasts/{result['podcast_id']}")
        assert resp.status_code == 404

    def test_delete_podcast_removes_its_jobs(self):
        pid = client.post(
            "/api/podcasts",
            json={
                "url": "http://example.com/feed.xml",
            },
        ).json()[
            "podcast_id"
        ]  # auto-creates a fetch job
        other_pid = client.post(
            "/api/podcasts",
            json={
                "url": "http://example.com/other.xml",
            },
        ).json()["podcast_id"]

        client.delete(f"/api/podcasts/{pid}")

        jobs = client.get("/api/jobs").json()
        assert all(j["podcast_id"] != pid for j in jobs)
        assert any(j["podcast_id"] == other_pid for j in jobs)

    def test_update_podcast(self):
        pid = client.post(
            "/api/podcasts",
            json={
                "url": "http://example.com/feed.xml",
                "name": "Before",
            },
        ).json()["podcast_id"]
        resp = client.patch(
            f"/api/podcasts/{pid}",
            json={
                "title": "After",
                "category": "news",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "After"
        assert body["rss_url"] == "http://example.com/feed.xml"
        assert body["category"] == "news"

    def test_update_podcast_with_episodes(self):
        pid = client.post(
            "/api/podcasts",
            json={
                "url": "http://example.com/feed.xml",
                "name": "Before",
            },
        ).json()["podcast_id"]
        deps.get_db().add_episode(
            pid, "Ep 1", datetime.now(), "http://example.com/ep1.mp3"
        )
        resp = client.patch(f"/api/podcasts/{pid}", json={"category": "news"})
        assert resp.status_code == 200
        assert resp.json()["category"] == "news"

    def test_update_podcast_not_found(self):
        resp = client.patch("/api/podcasts/999", json={"title": "X"})
        assert resp.status_code == 404


# ------------------------------------------------------------------
# Episodes
# ------------------------------------------------------------------


class TestEpisodes:
    def _seed_episode(self):
        db = deps.get_db()
        pid = db.add_podcast("Pod", "http://example.com/rss")
        eid = db.add_episode(
            pid,
            "Ep 1",
            datetime.now(),
            "http://example.com/ep1.mp3",
            file_path="/tmp/audio.wav",
        )
        return pid, eid

    def test_list_episodes_empty(self):
        resp = client.get("/api/episodes")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_episodes(self):
        self._seed_episode()
        resp = client.get("/api/episodes")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_list_episodes_by_podcast(self):
        pid, _ = self._seed_episode()
        resp = client.get(f"/api/episodes?podcast_id={pid}")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_get_episode(self):
        _, eid = self._seed_episode()
        resp = client.get(f"/api/episodes/{eid}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "Ep 1"

    def test_get_episode_not_found(self):
        resp = client.get("/api/episodes/999")
        assert resp.status_code == 404

    def test_transcribe_wrong_status(self):
        _, eid = self._seed_episode()
        db = deps.get_db()
        db.update_episode_status(eid, "processed")
        resp = client.post(f"/api/episodes/{eid}/transcribe")
        assert resp.status_code == 400

    def test_digest_wrong_status(self):
        _, eid = self._seed_episode()
        # Episode is 'downloaded', not 'transcribed'
        resp = client.post(f"/api/episodes/{eid}/digest")
        assert resp.status_code == 400

    def test_synopsis_requires_existing_summary(self):
        db = deps.get_db()
        pid = db.add_podcast("Pod", "http://example.com/syn.rss")
        eid = db.add_episode(pid, "Ep", datetime.now(), "http://example.com/syn.mp3")
        with patch("p3.api.routers.episodes.job_runner"):
            resp = client.post(f"/api/episodes/{eid}/synopsis")
        assert resp.status_code == 400

    def test_synopsis_unknown_episode(self):
        resp = client.post("/api/episodes/999999/synopsis")
        assert resp.status_code == 404


# ------------------------------------------------------------------
# Batch pipeline triggers
# ------------------------------------------------------------------


class TestBatchProcessing:
    @pytest.fixture(autouse=True)
    def _no_run(self, monkeypatch):
        """Queue jobs but never execute them (no ML work in tests)."""
        from p3.api import job_queue

        self.enqueued = []
        monkeypatch.setattr(
            job_queue.job_runner,
            "enqueue",
            lambda fn, *a: self.enqueued.append((fn.__name__, a)),
        )

    def _seed(self, statuses):
        db = deps.get_db()
        pid = db.add_podcast("Pod", "http://example.com/rss")
        for i, st in enumerate(statuses):
            eid = db.add_episode(
                pid, f"Ep {i}", datetime.now(), f"http://example.com/ep{i}.mp3"
            )
            db.update_episode_status(eid, st)
        return pid

    def test_per_podcast_only_eligible_episodes_queued(self):
        pid = self._seed(["downloaded", "downloaded", "transcribed", "processed"])
        resp = client.post(f"/api/podcasts/{pid}/process/transcribe")
        assert resp.status_code == 200
        assert resp.json()["queued"] == 2
        assert len(self.enqueued) == 2

    def test_library_wide_digest(self):
        self._seed(["transcribed", "transcribed", "downloaded"])
        resp = client.post("/api/episodes/process/digest")
        assert resp.status_code == 200
        assert resp.json()["queued"] == 2

    def test_bad_step_is_400(self):
        pid = self._seed(["downloaded"])
        assert client.post(f"/api/podcasts/{pid}/process/bogus").status_code == 400
        assert client.post("/api/episodes/process/bogus").status_code == 400

    def test_unknown_podcast_is_404(self):
        assert client.post("/api/podcasts/999/process/transcribe").status_code == 404

    def test_pipeline_step_skips_processed(self):
        self._seed(["downloaded", "transcribed", "processed"])
        resp = client.post("/api/episodes/process/pipeline")
        assert resp.json()["queued"] == 2


# ------------------------------------------------------------------
# Jobs
# ------------------------------------------------------------------


class TestJobs:
    def test_list_jobs_empty(self):
        resp = client.get("/api/jobs")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_jobs_no_default_cap(self):
        db = deps.get_db()
        pid = db.add_podcast("Pod", "http://example.com/rss")
        for _ in range(60):
            db.create_job("fetch", podcast_id=pid)

        assert len(client.get("/api/jobs").json()) == 60
        assert len(client.get("/api/jobs?limit=10").json()) == 10

    def test_get_job_not_found(self):
        resp = client.get("/api/jobs/nonexistent")
        assert resp.status_code == 404

    def test_job_created_on_podcast_add(self):
        client.post("/api/podcasts", json={"url": "http://example.com/rss"})
        resp = client.get("/api/jobs")
        assert resp.status_code == 200
        jobs = resp.json()
        assert len(jobs) >= 1
        assert jobs[0]["job_type"] == "fetch"

    def test_retry_not_found(self):
        assert client.post("/api/jobs/nope/retry").status_code == 404

    def test_retry_rejects_non_failed(self):
        db = deps.get_db()
        pid = db.add_podcast("Pod", "http://example.com/rss")
        jid = db.create_job("fetch", podcast_id=pid)
        db.update_job(jid, status="completed")
        assert client.post(f"/api/jobs/{jid}/retry").status_code == 400

    def test_retry_fetch_creates_new_job(self, monkeypatch):
        from p3.api import job_queue

        monkeypatch.setattr(job_queue.job_runner, "enqueue", lambda *a, **k: None)
        db = deps.get_db()
        pid = db.add_podcast("Pod", "http://example.com/rss")
        jid = db.create_job("fetch", podcast_id=pid)
        db.update_job(jid, status="failed", error="boom")

        resp = client.post(f"/api/jobs/{jid}/retry")
        assert resp.status_code == 200
        new_id = resp.json()["job_id"]
        assert new_id != jid
        new_job = db.get_job(new_id)
        assert new_job["job_type"] == "fetch"
        assert new_job["podcast_id"] == pid
        assert new_job["status"] == "pending"
        # original stays as history
        assert db.get_job(jid)["status"] == "failed"

    def test_retry_transcribe_reuses_episode(self, monkeypatch):
        from p3.api import job_queue

        monkeypatch.setattr(job_queue.job_runner, "enqueue", lambda *a, **k: None)
        db = deps.get_db()
        pid = db.add_podcast("Pod", "http://example.com/rss")
        eid = db.add_episode(pid, "Ep", datetime.now(), "http://example.com/e.mp3")
        jid = db.create_job("transcribe", episode_id=eid, podcast_id=pid)
        db.update_job(jid, status="failed")

        resp = client.post(f"/api/jobs/{jid}/retry")
        assert resp.status_code == 200
        new_job = db.get_job(resp.json()["job_id"])
        assert new_job["job_type"] == "transcribe"
        assert new_job["episode_id"] == eid

    def test_retry_unsupported_type(self):
        db = deps.get_db()
        jid = db.create_job("export")
        db.update_job(jid, status="failed")
        resp = client.post(f"/api/jobs/{jid}/retry")
        assert resp.status_code == 400

    def test_retry_rejects_superseded(self, monkeypatch):
        from p3.api import job_queue

        monkeypatch.setattr(job_queue.job_runner, "enqueue", lambda *a, **k: None)
        db = deps.get_db()
        pid = db.add_podcast("Pod", "http://example.com/rss")
        eid = db.add_episode(pid, "Ep", datetime.now(), "http://example.com/e.mp3")
        jid = db.create_job("transcribe", episode_id=eid, podcast_id=pid)
        db.update_job(jid, status="failed")
        db.update_episode_status(eid, "processed")  # a later attempt succeeded

        assert client.post(f"/api/jobs/{jid}/retry").status_code == 400

    def test_retry_failed_all_dedups_and_skips(self, monkeypatch):
        from p3.api import job_queue

        calls = []
        monkeypatch.setattr(
            job_queue.job_runner,
            "enqueue",
            lambda fn, *a: calls.append((fn.__name__, a)),
        )
        db = deps.get_db()
        pid = db.add_podcast("Pod", "http://example.com/rss")
        e1 = db.add_episode(pid, "E1", datetime.now(), "http://example.com/1.mp3")
        e2 = db.add_episode(pid, "E2", datetime.now(), "http://example.com/2.mp3")

        # e1: two failed transcribe attempts, still 'downloaded' -> one retry
        for _ in range(2):
            j = db.create_job("transcribe", episode_id=e1, podcast_id=pid)
            db.update_job(j, status="failed")
        # e2: failed transcribe, but episode already processed -> skipped
        j = db.create_job("transcribe", episode_id=e2, podcast_id=pid)
        db.update_job(j, status="failed")
        db.update_episode_status(e2, "processed")
        # an unsupported failed job -> skipped
        j = db.create_job("export")
        db.update_job(j, status="failed")

        resp = client.post("/api/jobs/retry-failed")
        assert resp.status_code == 200
        assert resp.json()["queued"] == 1
        assert len(calls) == 1
        fn_name, args = calls[0]
        assert fn_name == "task_transcribe"
        assert args[1] == e1  # (new_job_id, episode_id)
        # every failed record considered — retried, superseded, or
        # unsupported — is cleared, whether or not it was retried
        assert db.get_failed_jobs() == []

    def test_retry_failed_all_empty(self):
        resp = client.post("/api/jobs/retry-failed")
        assert resp.status_code == 200
        assert resp.json()["queued"] == 0

    def test_clear_jobs_keeps_active(self):
        db = deps.get_db()
        pid = db.add_podcast("Pod", "http://example.com/rss")
        done = db.create_job("fetch", podcast_id=pid)
        db.update_job(done, status="completed")
        bad = db.create_job("fetch", podcast_id=pid)
        db.update_job(bad, status="failed")
        running = db.create_job("fetch", podcast_id=pid)
        db.update_job(running, status="running")

        resp = client.delete("/api/jobs")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 2
        remaining = {j["id"] for j in client.get("/api/jobs").json()}
        assert remaining == {running}

    def test_clear_failed_jobs_route(self):
        db = deps.get_db()
        pid = db.add_podcast("Pod", "http://example.com/rss")
        done = db.create_job("fetch", podcast_id=pid)
        db.update_job(done, status="completed")
        bad = db.create_job("fetch", podcast_id=pid)
        db.update_job(bad, status="failed")

        resp = client.delete("/api/jobs/failed")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 1
        remaining = {j["id"] for j in client.get("/api/jobs").json()}
        assert remaining == {done}

    def test_clear_jobs_empty(self):
        resp = client.delete("/api/jobs")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 0


# ------------------------------------------------------------------
# Transcripts & Summaries
# ------------------------------------------------------------------


class TestTranscriptsAndSummaries:
    def test_transcript_not_found(self):
        resp = client.get("/api/episodes/999/transcript")
        assert resp.status_code == 404

    def test_summary_not_found(self):
        resp = client.get("/api/episodes/999/summary")
        assert resp.status_code == 404

    def test_get_transcript(self):
        db = deps.get_db()
        pid = db.add_podcast("Pod", "http://example.com/rss")
        eid = db.add_episode(pid, "Ep", datetime.now(), "http://example.com/ep.mp3")
        db.add_transcript_segments(
            eid,
            [
                {
                    "start": 0,
                    "end": 5,
                    "text": "Hello",
                    "speaker": None,
                    "confidence": 0.9,
                }
            ],
        )
        resp = client.get(f"/api/episodes/{eid}/transcript")
        assert resp.status_code == 200
        segments = resp.json()
        assert len(segments) == 1

    def test_dedupe_transcripts_route(self):
        db = deps.get_db()
        pid = db.add_podcast("Pod", "http://example.com/rss")
        eid = db.add_episode(pid, "Ep", datetime.now(), "http://example.com/ep.mp3")
        db.conn.execute(
            "INSERT INTO transcripts (episode_id, timestamp_start, timestamp_end, text) "
            "VALUES (?, ?, ?, ?)",
            (eid, 0.0, 5.0, "Hello"),
        )
        db.conn.execute(
            "INSERT INTO transcripts (episode_id, timestamp_start, timestamp_end, text) "
            "VALUES (?, ?, ?, ?)",
            (eid, 0.0, 5.0, "Hello"),
        )

        resp = client.post("/api/episodes/transcripts/dedupe")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 1
        assert len(client.get(f"/api/episodes/{eid}/transcript").json()) == 1

    def test_get_summary(self):
        db = deps.get_db()
        pid = db.add_podcast("Pod", "http://example.com/rss")
        eid = db.add_episode(pid, "Ep", datetime.now(), "http://example.com/ep.mp3")
        db.add_summary(eid, ["AI"], ["tech"], ["quote"], ["Startup"], "Great ep.")
        resp = client.get(f"/api/episodes/{eid}/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["full_summary"] == "Great ep."
        assert "AI" in data["key_topics"]


# ------------------------------------------------------------------
# Blogs
# ------------------------------------------------------------------


class TestBlogs:
    def test_list_blogs(self):
        resp = client.get("/api/blogs")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_blog_not_found(self):
        resp = client.get("/api/blogs/nonexistent-slug-xyz")
        assert resp.status_code == 404


# ------------------------------------------------------------------
# LinkedIn posts
# ------------------------------------------------------------------


class TestLinkedIn:
    def test_list_linkedin_posts(self):
        resp = client.get("/api/linkedin")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_linkedin_post_not_found(self):
        resp = client.get("/api/linkedin/nonexistent-slug-xyz")
        assert resp.status_code == 404

    def test_create_linkedin_unknown_episode(self):
        resp = client.post("/api/linkedin", json={"episode_id": 999999})
        assert resp.status_code == 404


# ------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------


class TestSettings:
    def test_get_settings(self):
        resp = client.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert "settings" in data
        assert "feeds" in data

    def test_update_settings(self):
        resp = client.put(
            "/api/settings",
            json={"settings": {"llm_provider": "openai", "llm_model": "gpt-4"}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["settings"]["llm_provider"] == "openai"

        # Verify persistence
        resp2 = client.get("/api/settings")
        assert resp2.json()["settings"]["llm_provider"] == "openai"


class TestCrossOriginGuard:
    def test_foreign_origin_write_is_refused(self):
        resp = client.post(
            "/api/jobs/retry-failed", headers={"Origin": "https://evil.example"}
        )
        assert resp.status_code == 403

    def test_foreign_origin_read_is_allowed(self):
        resp = client.get("/api/stats", headers={"Origin": "https://evil.example"})
        assert resp.status_code == 200

    def test_same_host_origin_write_is_allowed(self):
        resp = client.delete("/api/jobs", headers={"Origin": "http://testserver"})
        assert resp.status_code == 200

    def test_dev_server_origin_write_is_allowed(self):
        resp = client.delete("/api/jobs", headers={"Origin": "http://localhost:5173"})
        assert resp.status_code == 200


# ------------------------------------------------------------------
# YouTube sources
# ------------------------------------------------------------------


class TestYouTubeSources:
    VID = "aaaaaaaaaaa"

    @pytest.fixture(autouse=True)
    def _no_jobs(self, monkeypatch):
        from p3.api import job_queue

        self.enqueued = []
        monkeypatch.setattr(
            job_queue.job_runner,
            "enqueue",
            lambda fn, *args: self.enqueued.append((fn.__name__, args)),
        )

    def _video_info(self, **extra):
        return {
            "id": self.VID,
            "title": "A talk",
            "live_status": "not_live",
            "media_type": "video",
            **extra,
        }

    def test_add_video(self, monkeypatch):
        from p3 import youtube

        monkeypatch.setattr(youtube, "get_video_info", lambda u: self._video_info())
        resp = client.post(
            "/api/podcasts", json={"url": f"https://youtu.be/{self.VID}"}
        )
        assert resp.status_code == 200
        podcast = client.get(f"/api/podcasts/{resp.json()['podcast_id']}").json()
        assert podcast["source_type"] == "youtube_video"
        assert podcast["title"] == "A talk"
        assert podcast["rss_url"] == f"https://www.youtube.com/watch?v={self.VID}"
        assert self.enqueued[0][0] == "task_fetch"

        dup = client.post(
            "/api/podcasts",
            json={"url": f"https://www.youtube.com/watch?v={self.VID}"},
        )
        assert dup.status_code == 409

    def test_video_already_in_a_channel_is_409(self, monkeypatch):
        from p3 import youtube

        monkeypatch.setattr(youtube, "get_video_info", lambda u: self._video_info())
        db = deps.get_db()
        pid = db.add_podcast("Chan", "https://www.youtube.com/channel/UC" + "a" * 22)
        db.add_episode(
            pid, "A talk", None, f"https://www.youtube.com/watch?v={self.VID}"
        )
        resp = client.post(
            "/api/podcasts", json={"url": f"https://youtu.be/{self.VID}"}
        )
        assert resp.status_code == 409

    def test_livestream_video_is_400(self, monkeypatch):
        from p3 import youtube

        monkeypatch.setattr(
            youtube,
            "get_video_info",
            lambda u: self._video_info(live_status="is_upcoming"),
        )
        resp = client.post(
            "/api/podcasts", json={"url": f"https://youtu.be/{self.VID}"}
        )
        assert resp.status_code == 400
        assert "livestream" in resp.json()["detail"]
        assert client.get("/api/podcasts").json() == []

    def test_shorts_url_is_400(self):
        resp = client.post(
            "/api/podcasts",
            json={"url": f"https://www.youtube.com/shorts/{self.VID}"},
        )
        assert resp.status_code == 400

    def test_lookup_failure_is_502(self, monkeypatch):
        from p3 import youtube

        def boom(u):
            raise youtube.YouTubeError("Sign in to confirm you're not a bot")

        monkeypatch.setattr(youtube, "resolve_channel", boom)
        resp = client.post("/api/podcasts", json={"url": "https://www.youtube.com/@x"})
        assert resp.status_code == 502
        assert "not a bot" in resp.json()["detail"]

    def test_add_channel(self, monkeypatch):
        from p3 import youtube

        canonical = "https://www.youtube.com/channel/UC" + "b" * 22
        monkeypatch.setattr(youtube, "resolve_channel", lambda u: (canonical, "Chan"))
        resp = client.post(
            "/api/podcasts",
            json={"url": "https://www.youtube.com/@chan", "category": "ai"},
        )
        assert resp.status_code == 200
        [podcast] = client.get("/api/podcasts").json()
        assert podcast["source_type"] == "youtube_channel"
        assert podcast["rss_url"] == canonical
        assert podcast["category"] == "ai"

    def test_playlist_queues_import_job(self):
        resp = client.post(
            "/api/podcasts",
            json={
                "url": "https://www.youtube.com/playlist?list=PLx&si=abc",
                "category": "talks",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["podcast_id"] is None
        job = client.get(f"/api/jobs/{data['job_id']}").json()
        assert job["job_type"] == "import_playlist"
        assert self.enqueued == [
            (
                "task_import_playlist",
                (
                    data["job_id"],
                    "https://www.youtube.com/playlist?list=PLx",
                    "talks",
                ),
            )
        ]
        # No source is created up front.
        assert client.get("/api/podcasts").json() == []

    def test_rss_sources_report_rss_type(self):
        client.post("/api/podcasts", json={"url": "http://example.com/feed.xml"})
        [podcast] = client.get("/api/podcasts").json()
        assert podcast["source_type"] == "rss"
