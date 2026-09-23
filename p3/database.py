"""Database layer using DuckDB for P³ storage."""

import json
import logging
import threading
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb

logger = logging.getLogger(__name__)


class P3Database:
    def __init__(self, db_path: str = "data/p3.duckdb"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._base_conn = duckdb.connect(str(self.db_path))
        self._local = threading.local()
        self._initialize_schema()

    @property
    def conn(self):
        """Per-thread cursor over the shared DuckDB connection.

        A single DuckDB connection object is not safe for concurrent use. Under
        FastAPI, sync endpoints run in threadpool workers, so each thread gets
        its own cursor (sharing the same underlying database) instead.
        """
        if self._base_conn is None:
            return None
        cursor = getattr(self._local, "cursor", None)
        if cursor is None:
            cursor = self._base_conn.cursor()
            self._local.cursor = cursor
        return cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _row_to_dict(self, row, description) -> Dict[str, Any]:
        """Map a result row to a dict using cursor column names."""
        return {desc[0]: val for desc, val in zip(description, row)}

    def _fetchall_as_dicts(self, cursor) -> List[Dict[str, Any]]:
        """Fetch all rows from a cursor as a list of dicts."""
        description = cursor.description
        return [self._row_to_dict(row, description) for row in cursor.fetchall()]

    def _fetchone_as_dict(self, cursor) -> Optional[Dict[str, Any]]:
        """Fetch one row from a cursor as a dict, or None."""
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row, cursor.description)

    def _initialize_schema(self):
        """Create database schema if not exists."""
        self.conn.execute("""
            CREATE SEQUENCE IF NOT EXISTS podcast_id_seq START 1
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS podcasts (
                id INTEGER PRIMARY KEY DEFAULT nextval('podcast_id_seq'),
                title VARCHAR NOT NULL,
                rss_url VARCHAR UNIQUE NOT NULL,
                category VARCHAR,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        self.conn.execute("""
            CREATE SEQUENCE IF NOT EXISTS episode_id_seq START 1
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY DEFAULT nextval('episode_id_seq'),
                podcast_id INTEGER REFERENCES podcasts(id),
                title VARCHAR NOT NULL,
                date TIMESTAMP,
                url VARCHAR UNIQUE NOT NULL,
                file_path VARCHAR,
                duration_seconds INTEGER,
                status VARCHAR DEFAULT 'downloaded',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        self.conn.execute("""
            CREATE SEQUENCE IF NOT EXISTS transcript_id_seq START 1
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS transcripts (
                id INTEGER PRIMARY KEY DEFAULT nextval('transcript_id_seq'),
                episode_id INTEGER REFERENCES episodes(id),
                speaker VARCHAR,
                timestamp_start REAL,
                timestamp_end REAL,
                text TEXT NOT NULL,
                confidence REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        self.conn.execute("""
            CREATE SEQUENCE IF NOT EXISTS summary_id_seq START 1
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS summaries (
                id INTEGER PRIMARY KEY DEFAULT nextval('summary_id_seq'),
                episode_id INTEGER REFERENCES episodes(id),
                key_topics JSON,
                themes JSON,
                quotes JSON,
                startups JSON,
                digest_date DATE,
                full_summary TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migration: long-form study-notes synopsis alongside full_summary.
        self.conn.execute(
            "ALTER TABLE summaries ADD COLUMN IF NOT EXISTS long_summary TEXT"
        )

        # Jobs table for background task tracking
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id VARCHAR PRIMARY KEY,
                episode_id INTEGER,
                job_type VARCHAR NOT NULL,
                status VARCHAR DEFAULT 'pending',
                progress REAL DEFAULT 0.0,
                message VARCHAR,
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP
            )
        """)
        # Migration: link jobs to a podcast (fetch jobs have no episode_id)
        self.conn.execute(
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS podcast_id INTEGER"
        )

        # Indexes for common query patterns.
        #
        # episodes.status is deliberately not indexed: DuckDB executes an
        # UPDATE of an indexed column as delete+reinsert, which violates the
        # foreign keys from transcripts/summaries once they reference the
        # episode. Older databases may still carry the index, so drop it.
        self.conn.execute("DROP INDEX IF EXISTS idx_episodes_status")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_episodes_url ON episodes(url)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_transcripts_episode_id ON transcripts(episode_id)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_summaries_digest_date ON summaries(digest_date)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_summaries_episode_id ON summaries(episode_id)"
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_episode_id ON jobs(episode_id)"
        )

        self._resync_sequences()

    def _resync_sequences(self):
        """Ensure each id sequence starts past its table's current max id.

        A hard-killed process can leave a sequence's persisted position
        behind the table's rows, so nextval() would hand out an existing id.

        Repair is only attempted when a sequence is actually behind. It takes
        four DDL statements (the column default depends on the sequence, so
        it must be detached first), and a process killed mid-way can leave a
        WAL that DuckDB cannot replay. Checking first keeps that window rare.
        """
        for table, seq in (
            ("podcasts", "podcast_id_seq"),
            ("episodes", "episode_id_seq"),
            ("transcripts", "transcript_id_seq"),
            ("summaries", "summary_id_seq"),
        ):
            max_id = self.conn.execute(
                f"SELECT COALESCE(MAX(id), 0) FROM {table}"
            ).fetchone()[0]
            needed = max_id + 1

            row = self.conn.execute(
                "SELECT last_value, start_value FROM duckdb_sequences() "
                "WHERE sequence_name = ?",
                [seq],
            ).fetchone()
            if row is None:
                continue  # sequence missing entirely; leave schema init to create it
            last_value, start_value = row
            next_from_seq = last_value + 1 if last_value is not None else start_value
            if needed <= next_from_seq:
                continue  # already healthy — no DDL needed

            self.conn.execute(f"ALTER TABLE {table} ALTER COLUMN id DROP DEFAULT")
            self.conn.execute(f"DROP SEQUENCE IF EXISTS {seq}")
            self.conn.execute(f"CREATE SEQUENCE {seq} START {needed}")
            self.conn.execute(
                f"ALTER TABLE {table} ALTER COLUMN id SET DEFAULT nextval('{seq}')"
            )

    def add_podcast(
        self, title: str, rss_url: str, category: Optional[str] = None
    ) -> int:
        """Add new podcast feed."""
        next_id = self.conn.execute("SELECT nextval('podcast_id_seq')").fetchone()[0]
        self.conn.execute(
            "INSERT INTO podcasts (id, title, rss_url, category) VALUES (?, ?, ?, ?)",
            (next_id, title, rss_url, category),
        )
        return next_id

    def get_podcast_by_url(self, rss_url: str) -> Optional[Dict[str, Any]]:
        """Get podcast by RSS URL."""
        cursor = self.conn.execute(
            "SELECT * FROM podcasts WHERE rss_url = ?", (rss_url,)
        )
        return self._fetchone_as_dict(cursor)

    def add_episode(
        self,
        podcast_id: int,
        title: str,
        date: datetime,
        url: str,
        file_path: Optional[str] = None,
    ) -> int:
        """Add new episode."""
        next_id = self.conn.execute("SELECT nextval('episode_id_seq')").fetchone()[0]
        self.conn.execute(
            """
            INSERT INTO episodes (id, podcast_id, title, date, url, file_path)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (next_id, podcast_id, title, date, url, file_path),
        )
        return next_id

    def episode_exists(self, url: str) -> bool:
        """Check if episode already exists."""
        result = self.conn.execute(
            "SELECT 1 FROM episodes WHERE url = ?", (url,)
        ).fetchone()
        return result is not None

    def get_episode_by_id(self, episode_id: int) -> Optional[Dict[str, Any]]:
        """Get a single episode by ID, joined with podcast title."""
        cursor = self.conn.execute(
            """
            SELECT e.*, p.title as podcast_title
            FROM episodes e
            JOIN podcasts p ON e.podcast_id = p.id
            WHERE e.id = ?
        """,
            (episode_id,),
        )
        return self._fetchone_as_dict(cursor)

    def get_episodes_by_status(self, status: str) -> List[Dict[str, Any]]:
        """Get episodes by processing status."""
        cursor = self.conn.execute(
            """
            SELECT e.*, p.title as podcast_title
            FROM episodes e
            JOIN podcasts p ON e.podcast_id = p.id
            WHERE e.status = ?
            ORDER BY e.date DESC
        """,
            (status,),
        )
        return self._fetchall_as_dicts(cursor)

    def get_episodes_by_date_and_status(
        self, target_date, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get episodes matching a publication date, optionally filtered by status.

        Pass status=None to return every episode published on that date.
        """
        if status is None:
            cursor = self.conn.execute(
                """
                SELECT e.*, p.title as podcast_title
                FROM episodes e
                JOIN podcasts p ON e.podcast_id = p.id
                WHERE CAST(e.date AS DATE) = ?
                ORDER BY e.date DESC
            """,
                (target_date,),
            )
        else:
            cursor = self.conn.execute(
                """
                SELECT e.*, p.title as podcast_title
                FROM episodes e
                JOIN podcasts p ON e.podcast_id = p.id
                WHERE CAST(e.date AS DATE) = ? AND e.status = ?
                ORDER BY e.date DESC
            """,
                (target_date, status),
            )
        return self._fetchall_as_dicts(cursor)

    def update_episode_status(self, episode_id: int, status: str):
        """Update episode processing status."""
        self.conn.execute(
            "UPDATE episodes SET status = ? WHERE id = ?", (status, episode_id)
        )

    def add_transcript_segments(self, episode_id: int, segments: List[Dict[str, Any]]):
        """Replace an episode's transcript segments with a fresh set.

        Existing segments are deleted first so that re-transcribing an
        episode (for example, after an interruption before its status was
        updated) is idempotent rather than duplicating every segment.
        """
        self.conn.execute("DELETE FROM transcripts WHERE episode_id = ?", (episode_id,))
        for segment in segments:
            self.conn.execute(
                """
                INSERT INTO transcripts (episode_id, speaker, timestamp_start, timestamp_end, text, confidence)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    episode_id,
                    segment.get("speaker"),
                    segment.get("start"),
                    segment.get("end"),
                    segment.get("text"),
                    segment.get("confidence"),
                ),
            )

    def get_transcripts_for_episode(self, episode_id: int) -> List[Dict[str, Any]]:
        """Get all transcript segments for an episode."""
        cursor = self.conn.execute(
            """
            SELECT * FROM transcripts WHERE episode_id = ?
            ORDER BY timestamp_start
        """,
            (episode_id,),
        )
        return self._fetchall_as_dicts(cursor)

    def dedupe_transcripts(self) -> int:
        """Remove duplicate transcript segments (same episode, timestamps,
        and text), keeping the earliest-inserted copy. Repairs databases
        created before add_transcript_segments became idempotent. Returns
        the number of rows deleted."""
        before = self.conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
        self.conn.execute("""
            DELETE FROM transcripts WHERE id NOT IN (
                SELECT MIN(id) FROM transcripts
                GROUP BY episode_id, timestamp_start, timestamp_end, text
            )
        """)
        after = self.conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
        return before - after

    def get_transcript_by_episode_id(self, episode_id: int) -> Optional[str]:
        """Get the full transcript text for an episode, or None if not transcribed."""
        cursor = self.conn.execute(
            """
            SELECT text FROM transcripts WHERE episode_id = ?
            ORDER BY timestamp_start
        """,
            (episode_id,),
        )
        rows = self._fetchall_as_dicts(cursor)

        if not rows:
            return None
        return "\n".join(row["text"] for row in rows)

    def add_summary(
        self,
        episode_id: int,
        key_topics: List[str],
        themes: List[str],
        quotes: List[str],
        startups: List[str],
        full_summary: str,
        digest_date: Optional[date] = None,
        long_summary: Optional[str] = None,
    ):
        """Replace this episode's summary with a fresh one.

        full_summary is the short overview; long_summary is the optional
        long-form synopsis. Any existing row is deleted first so re-digesting
        an episode never creates duplicates.
        """
        if digest_date is None:
            digest_date = datetime.now().date()

        self.conn.execute("DELETE FROM summaries WHERE episode_id = ?", (episode_id,))
        self.conn.execute(
            """
            INSERT INTO summaries
                (episode_id, key_topics, themes, quotes, startups, full_summary, digest_date, long_summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                episode_id,
                json.dumps(key_topics),
                json.dumps(themes),
                json.dumps(quotes),
                json.dumps(startups),
                full_summary,
                digest_date,
                long_summary,
            ),
        )

    def update_summary_long_summary(self, episode_id: int, long_summary: str):
        """Set the long-form synopsis on an episode's existing summary row,
        leaving the other fields untouched."""
        self.conn.execute(
            "UPDATE summaries SET long_summary = ? WHERE episode_id = ?",
            (long_summary, episode_id),
        )

    def get_summaries_by_date(self, date: datetime) -> List[Dict[str, Any]]:
        """Get all summaries for a specific date."""
        cursor = self.conn.execute(
            """
            SELECT s.*, e.title as episode_title, p.title as podcast_title
            FROM summaries s
            JOIN episodes e ON s.episode_id = e.id
            JOIN podcasts p ON e.podcast_id = p.id
            WHERE s.digest_date = ?
            ORDER BY p.title, e.title
        """,
            (date.date(),),
        )
        rows = self._fetchall_as_dicts(cursor)
        # Deserialize JSON columns
        for row in rows:
            for col in ("key_topics", "themes", "quotes", "startups"):
                if isinstance(row[col], str):
                    row[col] = json.loads(row[col])
        return rows

    # ------------------------------------------------------------------
    # Podcast helpers
    # ------------------------------------------------------------------

    def get_podcast_by_id(self, podcast_id: int) -> Optional[Dict[str, Any]]:
        """Get podcast by ID."""
        cursor = self.conn.execute("SELECT * FROM podcasts WHERE id = ?", (podcast_id,))
        return self._fetchone_as_dict(cursor)

    def get_all_podcasts(self) -> List[Dict[str, Any]]:
        """Get all podcasts."""
        cursor = self.conn.execute("SELECT * FROM podcasts ORDER BY created_at DESC")
        return self._fetchall_as_dicts(cursor)

    def update_podcast(
        self,
        podcast_id: int,
        title: Optional[str] = None,
        category: Optional[str] = None,
    ):
        """Update podcast fields. Only non-None arguments are changed.

        rss_url is deliberately not updatable here: DuckDB cannot update an
        indexed (UNIQUE) column on a row referenced by a foreign key once the
        podcast has episodes.
        """
        parts = []
        params: List[Any] = []
        if title is not None:
            parts.append("title = ?")
            params.append(title)
        if category is not None:
            parts.append("category = ?")
            params.append(category)
        if not parts:
            return
        params.append(podcast_id)
        self.conn.execute(
            f"UPDATE podcasts SET {', '.join(parts)} WHERE id = ?", params
        )

    def delete_podcast(self, podcast_id: int):
        """Delete a podcast and all related data."""
        # Delete in dependency order
        self.conn.execute(
            """
            DELETE FROM summaries WHERE episode_id IN
                (SELECT id FROM episodes WHERE podcast_id = ?)
        """,
            (podcast_id,),
        )
        self.conn.execute(
            """
            DELETE FROM transcripts WHERE episode_id IN
                (SELECT id FROM episodes WHERE podcast_id = ?)
        """,
            (podcast_id,),
        )
        # Jobs for this podcast (fetch jobs) or any of its episodes
        # (transcribe/digest/pipeline jobs) — must run before episodes are gone.
        self.conn.execute(
            """
            DELETE FROM jobs WHERE podcast_id = ? OR episode_id IN
                (SELECT id FROM episodes WHERE podcast_id = ?)
        """,
            (podcast_id, podcast_id),
        )
        self.conn.execute("DELETE FROM episodes WHERE podcast_id = ?", (podcast_id,))
        self.conn.execute("DELETE FROM podcasts WHERE id = ?", (podcast_id,))

    # ------------------------------------------------------------------
    # Episode helpers
    # ------------------------------------------------------------------

    def get_episodes_by_podcast(self, podcast_id: int) -> List[Dict[str, Any]]:
        """Get all episodes for a podcast."""
        cursor = self.conn.execute(
            """
            SELECT e.*, p.title as podcast_title
            FROM episodes e
            JOIN podcasts p ON e.podcast_id = p.id
            WHERE e.podcast_id = ?
            ORDER BY e.date DESC
        """,
            (podcast_id,),
        )
        return self._fetchall_as_dicts(cursor)

    def get_all_episodes(self) -> List[Dict[str, Any]]:
        """Get all episodes with podcast title."""
        cursor = self.conn.execute("""
            SELECT e.*, p.title as podcast_title
            FROM episodes e
            JOIN podcasts p ON e.podcast_id = p.id
            ORDER BY e.date DESC
        """)
        return self._fetchall_as_dicts(cursor)

    # ------------------------------------------------------------------
    # Summary helpers
    # ------------------------------------------------------------------

    def get_summary_by_episode(self, episode_id: int) -> Optional[Dict[str, Any]]:
        """Get summary for a specific episode."""
        cursor = self.conn.execute(
            """
            SELECT s.*, e.title as episode_title, p.title as podcast_title
            FROM summaries s
            JOIN episodes e ON s.episode_id = e.id
            JOIN podcasts p ON e.podcast_id = p.id
            WHERE s.episode_id = ?
            ORDER BY s.created_at DESC
            LIMIT 1
        """,
            (episode_id,),
        )
        row = self._fetchone_as_dict(cursor)
        if row:
            for col in ("key_topics", "themes", "quotes", "startups"):
                if isinstance(row[col], str):
                    row[col] = json.loads(row[col])
        return row

    def get_all_summaries(self) -> List[Dict[str, Any]]:
        """Get all summaries."""
        cursor = self.conn.execute("""
            SELECT s.*, e.title as episode_title, p.title as podcast_title
            FROM summaries s
            JOIN episodes e ON s.episode_id = e.id
            JOIN podcasts p ON e.podcast_id = p.id
            ORDER BY s.created_at DESC
        """)
        rows = self._fetchall_as_dicts(cursor)
        for row in rows:
            for col in ("key_topics", "themes", "quotes", "startups"):
                if isinstance(row[col], str):
                    row[col] = json.loads(row[col])
        return rows

    # ------------------------------------------------------------------
    # Job tracking
    # ------------------------------------------------------------------

    def create_job(
        self,
        job_type: str,
        episode_id: Optional[int] = None,
        podcast_id: Optional[int] = None,
    ) -> str:
        """Create a new background job. Returns the job ID."""
        job_id = str(uuid.uuid4())
        self.conn.execute(
            """
            INSERT INTO jobs (id, episode_id, podcast_id, job_type, status)
            VALUES (?, ?, ?, ?, 'pending')
        """,
            (job_id, episode_id, podcast_id, job_type),
        )
        return job_id

    def update_job(
        self,
        job_id: str,
        status: Optional[str] = None,
        progress: Optional[float] = None,
        message: Optional[str] = None,
        error: Optional[str] = None,
    ):
        """Update job status/progress."""
        parts = []
        params: List[Any] = []
        if status is not None:
            parts.append("status = ?")
            params.append(status)
            if status == "running":
                parts.append("started_at = CURRENT_TIMESTAMP")
            elif status in ("completed", "failed"):
                parts.append("completed_at = CURRENT_TIMESTAMP")
        if progress is not None:
            parts.append("progress = ?")
            params.append(progress)
        if message is not None:
            parts.append("message = ?")
            params.append(message)
        if error is not None:
            parts.append("error = ?")
            params.append(error)
        if not parts:
            return
        params.append(job_id)
        self.conn.execute(f"UPDATE jobs SET {', '.join(parts)} WHERE id = ?", params)

    def fail_stale_jobs(self) -> int:
        """Mark jobs left 'pending'/'running' by a previous process as failed.

        The job runner is in-process, so anything not finished when the server
        stopped will never resume. Returns the number of rows updated.
        """
        cursor = self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('pending', 'running')"
        )
        count = cursor.fetchone()[0]
        if count:
            self.conn.execute(
                "UPDATE jobs SET status = 'failed', "
                "error = 'interrupted: server restarted', "
                "completed_at = CURRENT_TIMESTAMP "
                "WHERE status IN ('pending', 'running')"
            )
        return count

    # A job's podcast/episode names for display. Fetch jobs carry podcast_id
    # directly; per-episode jobs resolve it through the episode.
    _JOBS_SELECT = """
        SELECT j.*,
               e.title AS episode_title,
               p.title AS podcast_title
        FROM jobs j
        LEFT JOIN episodes e ON e.id = j.episode_id
        LEFT JOIN podcasts p ON p.id = COALESCE(j.podcast_id, e.podcast_id)
    """

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get a single job by ID."""
        cursor = self.conn.execute(self._JOBS_SELECT + " WHERE j.id = ?", (job_id,))
        return self._fetchone_as_dict(cursor)

    def get_recent_jobs(self, limit: Optional[int] = 20) -> List[Dict[str, Any]]:
        """Get recent jobs ordered by creation time. limit=None (or <=0)
        returns all of them."""
        sql = self._JOBS_SELECT + " ORDER BY j.created_at DESC"
        if limit is not None and limit > 0:
            cursor = self.conn.execute(sql + " LIMIT ?", (limit,))
        else:
            cursor = self.conn.execute(sql)
        return self._fetchall_as_dicts(cursor)

    def get_active_jobs(self) -> List[Dict[str, Any]]:
        """Get all pending or running jobs."""
        cursor = self.conn.execute(
            self._JOBS_SELECT
            + " WHERE j.status IN ('pending', 'running') ORDER BY j.created_at"
        )
        return self._fetchall_as_dicts(cursor)

    def get_failed_jobs(self) -> List[Dict[str, Any]]:
        """Get all failed jobs, newest first."""
        cursor = self.conn.execute(
            self._JOBS_SELECT + " WHERE j.status = 'failed' ORDER BY j.created_at DESC"
        )
        return self._fetchall_as_dicts(cursor)

    def clear_finished_jobs(self) -> int:
        """Delete completed and failed jobs. Pending/running jobs are kept."""
        cursor = self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('completed', 'failed')"
        )
        count = cursor.fetchone()[0]
        if count:
            self.conn.execute(
                "DELETE FROM jobs WHERE status IN ('completed', 'failed')"
            )
        return count

    def clear_failed_jobs(self) -> int:
        """Delete only failed jobs."""
        cursor = self.conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'failed'")
        count = cursor.fetchone()[0]
        if count:
            self.conn.execute("DELETE FROM jobs WHERE status = 'failed'")
        return count

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, int]:
        """Get pipeline statistics."""
        stats = {}
        stats["total_podcasts"] = self.conn.execute(
            "SELECT COUNT(*) FROM podcasts"
        ).fetchone()[0]
        stats["total_episodes"] = self.conn.execute(
            "SELECT COUNT(*) FROM episodes"
        ).fetchone()[0]
        for status in ("downloaded", "transcribed", "processed"):
            stats[f"episodes_{status}"] = self.conn.execute(
                "SELECT COUNT(*) FROM episodes WHERE status = ?", (status,)
            ).fetchone()[0]
        stats["total_summaries"] = self.conn.execute(
            "SELECT COUNT(*) FROM summaries"
        ).fetchone()[0]
        stats["active_jobs"] = self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('pending', 'running')"
        ).fetchone()[0]
        stats["running_jobs"] = self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'running'"
        ).fetchone()[0]
        stats["queued_jobs"] = self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'pending'"
        ).fetchone()[0]
        return stats

    def close(self):
        """Close database connection."""
        if self._base_conn is not None:
            self._base_conn.close()
            self._base_conn = None
            self._local = threading.local()
