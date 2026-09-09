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


# ------------------------------------------------------------------
# Podcasts
# ------------------------------------------------------------------

class TestPodcasts:
    def test_list_empty(self):
        resp = client.get("/api/podcasts")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_add_podcast(self):
        resp = client.post("/api/podcasts", json={
            "url": "http://example.com/feed.xml",
            "name": "Test Podcast",
            "category": "tech",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "podcast_id" in data
        assert "job_id" in data

    def test_add_duplicate_podcast(self):
        client.post("/api/podcasts", json={"url": "http://example.com/feed.xml"})
        resp = client.post("/api/podcasts", json={"url": "http://example.com/feed.xml"})
        assert resp.status_code == 409

    def test_get_podcast(self):
        result = client.post("/api/podcasts", json={
            "url": "http://example.com/feed.xml",
            "name": "My Pod",
        }).json()
        resp = client.get(f"/api/podcasts/{result['podcast_id']}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "My Pod"

    def test_get_podcast_not_found(self):
        resp = client.get("/api/podcasts/999")
        assert resp.status_code == 404

    def test_delete_podcast(self):
        result = client.post("/api/podcasts", json={
            "url": "http://example.com/feed.xml",
        }).json()
        resp = client.delete(f"/api/podcasts/{result['podcast_id']}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

        resp = client.get(f"/api/podcasts/{result['podcast_id']}")
        assert resp.status_code == 404


# ------------------------------------------------------------------
# Episodes
# ------------------------------------------------------------------

class TestEpisodes:
    def _seed_episode(self):
        db = deps.get_db()
        pid = db.add_podcast("Pod", "http://example.com/rss")
        eid = db.add_episode(pid, "Ep 1", datetime.now(), "http://example.com/ep1.mp3",
                             file_path="/tmp/audio.wav")
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


# ------------------------------------------------------------------
# Jobs
# ------------------------------------------------------------------

class TestJobs:
    def test_list_jobs_empty(self):
        resp = client.get("/api/jobs")
        assert resp.status_code == 200
        assert resp.json() == []

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
        db.add_transcript_segments(eid, [
            {"start": 0, "end": 5, "text": "Hello", "speaker": None, "confidence": 0.9}
        ])
        resp = client.get(f"/api/episodes/{eid}/transcript")
        assert resp.status_code == 200
        segments = resp.json()
        assert len(segments) == 1
        assert segments[0]["text"] == "Hello"

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
        resp = client.put("/api/settings", json={
            "settings": {"llm_provider": "openai", "llm_model": "gpt-4"}
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["settings"]["llm_provider"] == "openai"

        # Verify persistence
        resp2 = client.get("/api/settings")
        assert resp2.json()["settings"]["llm_provider"] == "openai"
