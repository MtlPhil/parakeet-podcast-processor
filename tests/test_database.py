"""Tests for P3Database."""

import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from p3.database import P3Database


@pytest.fixture
def db(tmp_path):
    """Create a temporary database for each test."""
    db_path = str(tmp_path / "test.duckdb")
    database = P3Database(db_path)
    yield database
    database.close()


class TestContextManager:
    def test_context_manager_closes(self, tmp_path):
        db_path = str(tmp_path / "ctx.duckdb")
        with P3Database(db_path) as database:
            database.add_podcast("Test", "http://example.com/feed", "tech")
        assert database.conn is None

    def test_context_manager_usable(self, tmp_path):
        db_path = str(tmp_path / "ctx2.duckdb")
        with P3Database(db_path) as database:
            pid = database.add_podcast("Test", "http://example.com/feed", "tech")
            assert pid >= 1


class TestPodcasts:
    def test_add_podcast(self, db):
        pid = db.add_podcast("My Podcast", "http://example.com/rss", "tech")
        assert pid >= 1

    def test_get_podcast_by_url(self, db):
        db.add_podcast("My Podcast", "http://example.com/rss", "tech")
        result = db.get_podcast_by_url("http://example.com/rss")
        assert result is not None
        assert result['title'] == "My Podcast"
        assert result['rss_url'] == "http://example.com/rss"
        assert result['category'] == "tech"

    def test_get_podcast_by_url_not_found(self, db):
        result = db.get_podcast_by_url("http://nonexistent.com/rss")
        assert result is None

    def test_duplicate_url_raises(self, db):
        db.add_podcast("Podcast 1", "http://example.com/rss", "tech")
        with pytest.raises(Exception):
            db.add_podcast("Podcast 2", "http://example.com/rss", "tech")


class TestEpisodes:
    def test_add_and_check_episode(self, db):
        pid = db.add_podcast("Pod", "http://example.com/rss")
        eid = db.add_episode(pid, "Episode 1", datetime.now(), "http://example.com/ep1.mp3")
        assert eid >= 1
        assert db.episode_exists("http://example.com/ep1.mp3")
        assert not db.episode_exists("http://example.com/nonexistent.mp3")

    def test_get_episode_by_id(self, db):
        pid = db.add_podcast("Pod", "http://example.com/rss")
        eid = db.add_episode(pid, "Episode 1", datetime.now(), "http://example.com/ep1.mp3",
                             file_path="/tmp/audio.wav")
        result = db.get_episode_by_id(eid)
        assert result is not None
        assert result['title'] == "Episode 1"
        assert result['podcast_title'] == "Pod"
        assert result['file_path'] == "/tmp/audio.wav"

    def test_get_episode_by_id_not_found(self, db):
        result = db.get_episode_by_id(9999)
        assert result is None

    def test_get_episodes_by_status(self, db):
        pid = db.add_podcast("Pod", "http://example.com/rss")
        db.add_episode(pid, "Ep 1", datetime.now(), "http://example.com/ep1.mp3")
        db.add_episode(pid, "Ep 2", datetime.now(), "http://example.com/ep2.mp3")

        downloaded = db.get_episodes_by_status('downloaded')
        assert len(downloaded) == 2

        transcribed = db.get_episodes_by_status('transcribed')
        assert len(transcribed) == 0

    def test_update_episode_status(self, db):
        pid = db.add_podcast("Pod", "http://example.com/rss")
        eid = db.add_episode(pid, "Ep 1", datetime.now(), "http://example.com/ep1.mp3")

        db.update_episode_status(eid, 'transcribed')

        downloaded = db.get_episodes_by_status('downloaded')
        assert len(downloaded) == 0

        transcribed = db.get_episodes_by_status('transcribed')
        assert len(transcribed) == 1
        assert transcribed[0]['title'] == "Ep 1"


class TestTranscripts:
    def test_add_and_get_transcripts(self, db):
        pid = db.add_podcast("Pod", "http://example.com/rss")
        eid = db.add_episode(pid, "Ep 1", datetime.now(), "http://example.com/ep1.mp3")

        segments = [
            {"start": 0.0, "end": 5.0, "text": "Hello world", "speaker": None, "confidence": 0.95},
            {"start": 5.0, "end": 10.0, "text": "Testing", "speaker": "Speaker1", "confidence": 0.88},
        ]
        db.add_transcript_segments(eid, segments)

        result = db.get_transcripts_for_episode(eid)
        assert len(result) == 2
        assert result[0]['text'] == "Hello world"
        assert result[1]['speaker'] == "Speaker1"
        # Check ordering by timestamp
        assert result[0]['timestamp_start'] <= result[1]['timestamp_start']


class TestSummaries:
    def test_add_and_get_summary(self, db):
        pid = db.add_podcast("Pod", "http://example.com/rss")
        now = datetime.now()
        eid = db.add_episode(pid, "Ep 1", now, "http://example.com/ep1.mp3")

        db.add_summary(
            episode_id=eid,
            key_topics=["AI", "ML"],
            themes=["technology"],
            quotes=["Great quote here"],
            startups=["StartupCo"],
            full_summary="A great episode about AI.",
            digest_date=now
        )

        summaries = db.get_summaries_by_date(now)
        assert len(summaries) == 1
        s = summaries[0]
        assert s['key_topics'] == ["AI", "ML"]
        assert s['themes'] == ["technology"]
        assert s['quotes'] == ["Great quote here"]
        assert s['startups'] == ["StartupCo"]
        assert s['full_summary'] == "A great episode about AI."
        assert s['episode_title'] == "Ep 1"
        assert s['podcast_title'] == "Pod"

    def test_no_summaries_for_date(self, db):
        result = db.get_summaries_by_date(datetime(2020, 1, 1))
        assert result == []


class TestRowMapping:
    """Verify that _row_to_dict produces correct keys regardless of column order."""

    def test_dict_keys_match_columns(self, db):
        pid = db.add_podcast("Pod", "http://example.com/rss", "tech")
        result = db.get_podcast_by_url("http://example.com/rss")
        expected_keys = {'id', 'title', 'rss_url', 'category', 'created_at'}
        assert set(result.keys()) == expected_keys
