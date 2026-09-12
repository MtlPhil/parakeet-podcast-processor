"""Tests for P3Database."""

import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

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

    def test_update_podcast(self, db):
        pid = db.add_podcast("Old", "http://example.com/rss", "tech")
        db.update_podcast(pid, title="New", category="business")
        result = db.get_podcast_by_id(pid)
        assert result['title'] == "New"
        assert result['rss_url'] == "http://example.com/rss"
        assert result['category'] == "business"

    def test_update_podcast_partial(self, db):
        pid = db.add_podcast("Keep", "http://example.com/rss", "tech")
        db.update_podcast(pid, category="news")
        result = db.get_podcast_by_id(pid)
        assert result['title'] == "Keep"
        assert result['category'] == "news"

    def test_update_podcast_with_episodes(self, db):
        pid = db.add_podcast("Has eps", "http://example.com/rss", "tech")
        db.add_episode(pid, "Ep 1", datetime.now(), "http://example.com/ep1.mp3")
        db.update_podcast(pid, title="Renamed", category="news")
        result = db.get_podcast_by_id(pid)
        assert result['title'] == "Renamed"
        assert result['category'] == "news"

    def test_update_podcast_noop(self, db):
        pid = db.add_podcast("Same", "http://example.com/rss", "tech")
        db.update_podcast(pid)
        result = db.get_podcast_by_id(pid)
        assert result['title'] == "Same"

    def test_delete_podcast_removes_its_jobs(self, db):
        pid = db.add_podcast("Gone", "http://example.com/rss")
        eid = db.add_episode(pid, "Ep 1", datetime.now(), "http://example.com/ep1.mp3")
        fetch_job = db.create_job("fetch", podcast_id=pid)
        transcribe_job = db.create_job("transcribe", episode_id=eid, podcast_id=pid)

        other_pid = db.add_podcast("Stays", "http://example.com/other")
        other_job = db.create_job("fetch", podcast_id=other_pid)

        db.delete_podcast(pid)

        assert db.get_job(fetch_job) is None
        assert db.get_job(transcribe_job) is None
        assert db.get_job(other_job) is not None


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

    def test_add_transcript_segments_replaces_not_appends(self, db):
        """Regression test: a retried transcription (e.g. after an
        interrupted prior attempt) must not duplicate segments."""
        pid = db.add_podcast("Pod", "http://example.com/rss")
        eid = db.add_episode(pid, "Ep 1", datetime.now(), "http://example.com/ep1.mp3")

        segments = [{"start": 0.0, "end": 5.0, "text": "Hello", "speaker": None, "confidence": 0.9}]
        db.add_transcript_segments(eid, segments)
        db.add_transcript_segments(eid, segments)  # simulate a retranscription

        result = db.get_transcripts_for_episode(eid)
        assert len(result) == 1

    def test_dedupe_transcripts_removes_existing_duplicates(self, db):
        pid = db.add_podcast("Pod", "http://example.com/rss")
        e1 = db.add_episode(pid, "Ep 1", datetime.now(), "http://example.com/1.mp3")
        e2 = db.add_episode(pid, "Ep 2", datetime.now(), "http://example.com/2.mp3")

        # e1 has a duplicated set (simulating the old bug), e2 is untouched
        seg = {"start": 0.0, "end": 5.0, "text": "Hello", "speaker": None, "confidence": 0.9}
        db.conn.execute(
            "INSERT INTO transcripts (episode_id, speaker, timestamp_start, timestamp_end, text, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (e1, None, 0.0, 5.0, "Hello", 0.9),
        )
        db.conn.execute(
            "INSERT INTO transcripts (episode_id, speaker, timestamp_start, timestamp_end, text, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (e1, None, 0.0, 5.0, "Hello", 0.9),
        )
        db.add_transcript_segments(e2, [seg])

        deleted = db.dedupe_transcripts()
        assert deleted == 1
        assert len(db.get_transcripts_for_episode(e1)) == 1
        assert len(db.get_transcripts_for_episode(e2)) == 1

    def test_status_updates_after_transcript_and_summary_written(self, db):
        """Regression test: episodes.status must stay updatable after a
        transcript/summary references the row. DuckDB implements an UPDATE of
        an indexed column as delete+reinsert, which used to fail here with a
        foreign key ConstraintException the moment a child row existed —
        every real transcribe/digest job hit this. There must be no index on
        episodes(status) (see _initialize_schema)."""
        pid = db.add_podcast("Pod", "http://example.com/rss")
        eid = db.add_episode(pid, "Ep 1", datetime.now(), "http://example.com/ep1.mp3")

        db.add_transcript_segments(eid, [
            {"start": 0.0, "end": 1.0, "text": "hi", "speaker": None, "confidence": 0.9}
        ])
        db.update_episode_status(eid, 'transcribed')  # this used to raise
        assert db.get_episode_by_id(eid)['status'] == 'transcribed'

        db.add_summary(eid, key_topics=[], themes=[], quotes=[], startups=[],
                       full_summary="summary", digest_date=datetime.now())
        db.update_episode_status(eid, 'processed')  # and so did this
        assert db.get_episode_by_id(eid)['status'] == 'processed'


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
        assert s['long_summary'] is None  # not provided above

    def test_summary_stores_long_summary(self, db):
        pid = db.add_podcast("Pod", "http://example.com/rss")
        now = datetime.now()
        eid = db.add_episode(pid, "Ep 1", now, "http://example.com/ep1.mp3")

        db.add_summary(
            episode_id=eid,
            key_topics=[], themes=[], quotes=[], startups=[],
            full_summary="Short version.",
            long_summary="Much longer section-by-section notes.",
            digest_date=now
        )

        s = db.get_summaries_by_date(now)[0]
        assert s['full_summary'] == "Short version."
        assert s['long_summary'] == "Much longer section-by-section notes."

    def test_add_summary_replaces_not_duplicates(self, db):
        """Regression test: redigesting an episode (e.g. backfilling
        long_summary onto one processed before that field existed) must
        replace its summary, not add a second row."""
        pid = db.add_podcast("Pod", "http://example.com/rss")
        now = datetime.now()
        eid = db.add_episode(pid, "Ep 1", now, "http://example.com/ep1.mp3")

        db.add_summary(episode_id=eid, key_topics=[], themes=[], quotes=[],
                       startups=[], full_summary="First pass.", digest_date=now)
        db.add_summary(episode_id=eid, key_topics=[], themes=[], quotes=[],
                       startups=[], full_summary="Second pass.",
                       long_summary="Now with notes.", digest_date=now)

        summaries = db.get_summaries_by_date(now)
        assert len(summaries) == 1
        assert summaries[0]['full_summary'] == "Second pass."
        assert summaries[0]['long_summary'] == "Now with notes."

    def test_no_summaries_for_date(self, db):
        result = db.get_summaries_by_date(datetime(2020, 1, 1))
        assert result == []


class TestJobs:
    def test_create_job_with_podcast_id(self, db):
        pid = db.add_podcast("Pod", "http://example.com/rss")
        job_id = db.create_job("fetch", podcast_id=pid)
        job = db.get_job(job_id)
        assert job["podcast_id"] == pid
        assert job["podcast_title"] == "Pod"
        assert job["episode_title"] is None

    def test_job_resolves_podcast_through_episode(self, db):
        pid = db.add_podcast("Pod", "http://example.com/rss")
        eid = db.add_episode(pid, "Ep 1", datetime.now(), "http://example.com/ep1.mp3")
        job_id = db.create_job("transcribe", episode_id=eid, podcast_id=pid)
        job = db.get_recent_jobs()[0]
        assert job["episode_title"] == "Ep 1"
        assert job["podcast_title"] == "Pod"

    def test_get_recent_jobs_limit_none_returns_all(self, db):
        pid = db.add_podcast("Pod", "http://example.com/rss")
        for _ in range(5):
            db.create_job("fetch", podcast_id=pid)

        assert len(db.get_recent_jobs(limit=None)) == 5
        assert len(db.get_recent_jobs(limit=0)) == 5
        assert len(db.get_recent_jobs(limit=2)) == 2

    def test_fail_stale_jobs(self, db):
        pid = db.add_podcast("Pod", "http://example.com/rss")
        j1 = db.create_job("fetch", podcast_id=pid)
        j2 = db.create_job("fetch", podcast_id=pid)
        db.update_job(j2, status="running")
        j3 = db.create_job("fetch", podcast_id=pid)
        db.update_job(j3, status="completed")

        n = db.fail_stale_jobs()
        assert n == 2
        assert db.get_job(j1)["status"] == "failed"
        assert db.get_job(j2)["status"] == "failed"
        assert db.get_job(j3)["status"] == "completed"
        assert db.fail_stale_jobs() == 0

    def test_get_failed_jobs(self, db):
        pid = db.add_podcast("Pod", "http://example.com/rss")
        ok = db.create_job("fetch", podcast_id=pid)
        db.update_job(ok, status="completed")
        bad = db.create_job("fetch", podcast_id=pid)
        db.update_job(bad, status="failed")

        failed = db.get_failed_jobs()
        assert [j["id"] for j in failed] == [bad]

    def test_clear_finished_jobs_keeps_active(self, db):
        pid = db.add_podcast("Pod", "http://example.com/rss")
        done = db.create_job("fetch", podcast_id=pid)
        db.update_job(done, status="completed")
        bad = db.create_job("fetch", podcast_id=pid)
        db.update_job(bad, status="failed")
        running = db.create_job("fetch", podcast_id=pid)
        db.update_job(running, status="running")
        pending = db.create_job("fetch", podcast_id=pid)

        n = db.clear_finished_jobs()
        assert n == 2
        assert db.get_job(done) is None
        assert db.get_job(bad) is None
        assert db.get_job(running) is not None
        assert db.get_job(pending) is not None
        assert db.clear_finished_jobs() == 0

    def test_clear_failed_jobs_only(self, db):
        pid = db.add_podcast("Pod", "http://example.com/rss")
        done = db.create_job("fetch", podcast_id=pid)
        db.update_job(done, status="completed")
        bad = db.create_job("fetch", podcast_id=pid)
        db.update_job(bad, status="failed")

        n = db.clear_failed_jobs()
        assert n == 1
        assert db.get_job(bad) is None
        assert db.get_job(done) is not None


class TestSequenceResync:
    """A hard-killed process can leave a sequence behind the table's real max
    id (see incident notes in _resync_sequences); this must self-heal on the
    next connect instead of raising a duplicate-key error on insert."""

    def test_resync_fixes_sequence_left_behind_max_id(self, tmp_path):
        db_path = str(tmp_path / "test.duckdb")
        db = P3Database(db_path)
        p1 = db.add_podcast("A", "http://example.com/a")
        p2 = db.add_podcast("B", "http://example.com/b")
        assert p2 > p1

        # Simulate the drift a hard kill can leave: sequence reset behind
        # the table's current rows. (DuckDB ties the column DEFAULT to the
        # sequence, so — like _resync_sequences itself — the default has to
        # come off before the sequence can be replaced.)
        db.conn.execute("ALTER TABLE podcasts ALTER COLUMN id DROP DEFAULT")
        db.conn.execute("DROP SEQUENCE IF EXISTS podcast_id_seq")
        db.conn.execute("CREATE SEQUENCE podcast_id_seq START 1")
        db.conn.execute(
            "ALTER TABLE podcasts ALTER COLUMN id SET DEFAULT nextval('podcast_id_seq')"
        )
        db.close()

        # Reopening (a fresh backend start) must resync past the existing rows.
        db2 = P3Database(db_path)
        p3 = db2.add_podcast("C", "http://example.com/c")
        assert p3 > p2
        db2.close()

    def test_resync_is_a_noop_on_healthy_sequence(self, tmp_path):
        db_path = str(tmp_path / "test.duckdb")
        db = P3Database(db_path)
        p1 = db.add_podcast("A", "http://example.com/a")
        db.close()

        db2 = P3Database(db_path)
        p2 = db2.add_podcast("B", "http://example.com/b")
        assert p2 == p1 + 1
        db2.close()

    def test_resync_issues_no_ddl_when_healthy(self, tmp_path):
        """The repair (DROP DEFAULT / DROP SEQUENCE / CREATE SEQUENCE / SET
        DEFAULT) is a real DuckDB-WAL-replay risk if interrupted (see
        _resync_sequences docstring), so the common healthy case must not
        touch DDL at all — only read duckdb_sequences()."""
        db_path = str(tmp_path / "test.duckdb")
        db = P3Database(db_path)
        db.add_podcast("A", "http://example.com/a")
        db.close()

        db2 = P3Database(db_path)
        # conn is a thread-local cursor (see property in database.py); swap
        # it for a wrapping spy so calls still hit the real connection.
        spy = MagicMock(wraps=db2.conn)
        db2._local.cursor = spy

        db2._resync_sequences()

        ddl_calls = [
            call.args[0] for call in spy.execute.call_args_list
            if call.args and call.args[0].strip().upper().split()[0]
            in ("ALTER", "DROP", "CREATE")
        ]
        assert ddl_calls == []
        db2.close()


class TestRowMapping:
    """Verify that _row_to_dict produces correct keys regardless of column order."""

    def test_dict_keys_match_columns(self, db):
        pid = db.add_podcast("Pod", "http://example.com/rss", "tech")
        result = db.get_podcast_by_url("http://example.com/rss")
        expected_keys = {'id', 'title', 'rss_url', 'category', 'created_at'}
        assert set(result.keys()) == expected_keys


class TestStats:
    def test_running_and_queued_split_out(self, db):
        pid = db.add_podcast("Pod", "http://example.com/rss")
        db.create_job("fetch", podcast_id=pid)  # pending
        db.create_job("fetch", podcast_id=pid)  # pending
        running = db.create_job("fetch", podcast_id=pid)
        db.update_job(running, status="running")
        done = db.create_job("fetch", podcast_id=pid)
        db.update_job(done, status="completed")

        stats = db.get_stats()
        assert stats['queued_jobs'] == 2
        assert stats['running_jobs'] == 1
        assert stats['active_jobs'] == 3
