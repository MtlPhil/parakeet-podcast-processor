"""Database layer using DuckDB for P³ storage."""

import json
import logging
import threading
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb

logger = logging.getLogger(__name__)


# Error recorded on jobs a server restart cut short (see fail_stale_jobs).
INTERRUPTED_ERROR = "interrupted: server restarted"


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

        # Migration: what kind of source this row is -- 'rss' (podcast feed),
        # 'youtube_channel' or 'youtube_video'. rss_url holds the source's
        # canonical URL whatever its type.
        self.conn.execute(
            "ALTER TABLE podcasts ADD COLUMN IF NOT EXISTS "
            "source_type VARCHAR DEFAULT 'rss'"
        )

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

        # Migration: language detected from the audio when it was transcribed
        # (ISO 639-1 code such as 'en' or 'fr'); NULL until then.
        self.conn.execute(
            "ALTER TABLE episodes ADD COLUMN IF NOT EXISTS language VARCHAR"
        )

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
        # Migration: group the jobs of one scheduled daily run
        self.conn.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS run_id VARCHAR")

        # Migration: who started a job (None for the daily run and the CLI)
        self.conn.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS username VARCHAR")
        # Migration: Quebec French versions of the digest, next to English
        for column, kind in (
            ("full_summary_fr", "TEXT"),
            ("long_summary_fr", "TEXT"),
            ("key_topics_fr", "JSON"),
            ("themes_fr", "JSON"),
        ):
            self.conn.execute(
                f"ALTER TABLE summaries ADD COLUMN IF NOT EXISTS {column} {kind}"
            )

        # Users, signed in through the reverse proxy. Podcasts and episodes
        # are shared rows processed once; these tables say who sees what.
        # A user sees the podcasts they subscribe to and the episodes linked
        # to them. Deleting an episode unlinks it and leaves a tombstone, so
        # the user's own fetches never bring it back unless hand-picked.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username VARCHAR PRIMARY KEY,
                display_name VARCHAR,
                email VARCHAR,
                role VARCHAR DEFAULT 'user',
                settings JSON,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Built-in sign-in (P3_AUTH=local, see p3/api/auth.py): a password
        # hash per user, and sessions keyed by the SHA-256 of their token.
        self.conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR"
        )
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash VARCHAR PRIMARY KEY,
                username VARCHAR NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS user_podcasts (
                username VARCHAR NOT NULL,
                podcast_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (username, podcast_id)
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS user_episodes (
                username VARCHAR NOT NULL,
                episode_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (username, episode_id)
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS user_deleted_episodes (
                username VARCHAR NOT NULL,
                url VARCHAR NOT NULL,
                PRIMARY KEY (username, url)
            )
        """)
        for column, kind in (
            ("title", "VARCHAR"),
            ("podcast_title", "VARCHAR"),
            ("deleted_at", "TIMESTAMP"),
        ):
            self.conn.execute(
                f"ALTER TABLE user_deleted_episodes ADD COLUMN IF NOT EXISTS "
                f"{column} {kind}"
            )
        # Migration: deletions used to be remembered by episode id, which kept
        # the whole episode in the database. They are now remembered by URL.
        has_old = self.conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'user_hidden_episodes'"
        ).fetchone()
        if has_old:
            self.conn.execute("""
                INSERT OR IGNORE INTO user_deleted_episodes (username, url, title)
                SELECT h.username, e.url, e.title FROM user_hidden_episodes h
                JOIN episodes e ON e.id = h.episode_id
            """)
            self.conn.execute("DROP TABLE user_hidden_episodes")

        # Small key-value store for app state that must survive restarts
        # (e.g. the date of the last scheduled daily run).
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS app_state (
                key VARCHAR PRIMARY KEY,
                value VARCHAR
            )
        """)

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
        A finished repair is checkpointed at once: DuckDB cannot replay a WAL
        holding ``SET DEFAULT nextval(...)`` at all, so until the next
        checkpoint a hard kill would leave a database that does not open.
        """
        repaired = False
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
            repaired = True
        if repaired:
            self.conn.execute("CHECKPOINT")

    def add_podcast(
        self,
        title: str,
        rss_url: str,
        category: Optional[str] = None,
        source_type: str = "rss",
    ) -> int:
        """Add a new source: a podcast feed, YouTube channel or YouTube video."""
        next_id = self.conn.execute("SELECT nextval('podcast_id_seq')").fetchone()[0]
        self.conn.execute(
            "INSERT INTO podcasts (id, title, rss_url, category, source_type) "
            "VALUES (?, ?, ?, ?, ?)",
            (next_id, title, rss_url, category, source_type),
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
        date: Optional[datetime],
        url: str,
        file_path: Optional[str] = None,
    ) -> int:
        """Add new episode.

        ``date`` is stored as naive UTC. The ``date`` column is DuckDB's
        timezone-naive TIMESTAMP, so binding a timezone-aware datetime (as
        RSS and YouTube publication dates are) silently converts it to the
        host machine's local wall-clock time instead -- making the stored
        value depend on whatever timezone P3 happens to run in.
        """
        if date is not None and date.tzinfo is not None:
            date = date.astimezone(timezone.utc).replace(tzinfo=None)
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

    def get_episodes_by_status(
        self, status: str, username: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get episodes by processing status (only a user's, if given)."""
        join, params = self._user_episode_join(username)
        cursor = self.conn.execute(
            f"""
            SELECT e.*, p.title as podcast_title
            FROM episodes e
            JOIN podcasts p ON e.podcast_id = p.id
            {join}
            WHERE e.status = ?
            ORDER BY e.date DESC
        """,
            params + [status],
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

    def set_episode_language(self, episode_id: int, language: Optional[str]):
        """Record the language detected in an episode's audio."""
        self.conn.execute(
            "UPDATE episodes SET language = ? WHERE id = ?", (language, episode_id)
        )

    def remove_audio(self, episode_id: int) -> int:
        """Delete an episode's audio file and clear its file_path, keeping
        the episode, transcript and summary. Returns the bytes freed."""
        row = self.conn.execute(
            "SELECT file_path FROM episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        if not row or not row[0]:
            return 0
        path = Path(row[0])
        freed = 0
        if path.exists():
            freed = path.stat().st_size
            path.unlink()
        self.conn.execute(
            "UPDATE episodes SET file_path = NULL WHERE id = ?", (episode_id,)
        )
        return freed

    def remove_processed_audio(self) -> Dict[str, int]:
        """Delete the audio of every processed episode that still has some.
        Returns the number of episodes cleaned and the bytes freed."""
        ids = [
            row[0]
            for row in self.conn.execute(
                "SELECT id FROM episodes "
                "WHERE status = 'processed' AND file_path IS NOT NULL"
            ).fetchall()
        ]
        freed = sum(self.remove_audio(episode_id) for episode_id in ids)
        return {"episodes": len(ids), "bytes_freed": freed}

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
        long_summary_fr: Optional[str] = None,
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
                (episode_id, key_topics, themes, quotes, startups, full_summary,
                 digest_date, long_summary, long_summary_fr)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                long_summary_fr,
            ),
        )

    def update_summary_long_summary(self, episode_id: int, long_summary: str):
        """Set the long-form synopsis on an episode's existing summary row,
        leaving the other fields untouched."""
        self.conn.execute(
            "UPDATE summaries SET long_summary = ? WHERE episode_id = ?",
            (long_summary, episode_id),
        )

    _FRENCH_SUMMARY_FIELDS = (
        "full_summary_fr",
        "long_summary_fr",
        "key_topics_fr",
        "themes_fr",
    )

    def update_summary_fields(self, episode_id: int, **fields: Any):
        """Set French fields on an episode's existing summary row (see
        _FRENCH_SUMMARY_FIELDS); list values are stored as JSON."""
        for column, value in fields.items():
            if column not in self._FRENCH_SUMMARY_FIELDS:
                raise ValueError(f"Not an updatable summary field: {column}")
            if isinstance(value, list):
                value = json.dumps(value, ensure_ascii=False)
            self.conn.execute(
                f"UPDATE summaries SET {column} = ? WHERE episode_id = ?",
                (value, episode_id),
            )

    def get_summaries_by_date(
        self, date: datetime, username: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get all summaries for a specific date (only a user's, if given)."""
        join, params = self._user_episode_join(username)
        cursor = self.conn.execute(
            f"""
            SELECT s.*, e.title as episode_title, p.title as podcast_title
            FROM summaries s
            JOIN episodes e ON s.episode_id = e.id
            JOIN podcasts p ON e.podcast_id = p.id
            {join}
            WHERE s.digest_date = ?
            ORDER BY p.title, e.title
        """,
            params + [date.date()],
        )
        rows = self._fetchall_as_dicts(cursor)
        for row in rows:
            self._decode_summary(row)
        return rows

    # ------------------------------------------------------------------
    # Podcast helpers
    # ------------------------------------------------------------------

    def get_podcast_by_id(self, podcast_id: int) -> Optional[Dict[str, Any]]:
        """Get podcast by ID."""
        cursor = self.conn.execute("SELECT * FROM podcasts WHERE id = ?", (podcast_id,))
        return self._fetchone_as_dict(cursor)

    def get_all_podcasts(self, username: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all podcasts, or only those a user subscribes to."""
        if username is None:
            cursor = self.conn.execute(
                "SELECT * FROM podcasts ORDER BY created_at DESC"
            )
        else:
            cursor = self.conn.execute(
                """
                SELECT p.* FROM podcasts p
                JOIN user_podcasts up ON up.podcast_id = p.id AND up.username = ?
                ORDER BY p.created_at DESC
            """,
                (username,),
            )
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
        self.conn.execute(
            "DELETE FROM user_deleted_episodes WHERE url IN "
            "(SELECT url FROM episodes WHERE podcast_id = ?)",
            (podcast_id,),
        )
        self.conn.execute(
            "DELETE FROM user_episodes WHERE episode_id IN "
            "(SELECT id FROM episodes WHERE podcast_id = ?)",
            (podcast_id,),
        )
        self.conn.execute(
            "DELETE FROM user_podcasts WHERE podcast_id = ?", (podcast_id,)
        )
        self.conn.execute("DELETE FROM episodes WHERE podcast_id = ?", (podcast_id,))
        self.conn.execute("DELETE FROM podcasts WHERE id = ?", (podcast_id,))

    # ------------------------------------------------------------------
    # Episode helpers
    # ------------------------------------------------------------------

    def get_episodes_by_podcast(
        self, podcast_id: int, username: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get all episodes for a podcast (only a user's, if given)."""
        join, params = self._user_episode_join(username)
        cursor = self.conn.execute(
            f"""
            SELECT e.*, p.title as podcast_title
            FROM episodes e
            JOIN podcasts p ON e.podcast_id = p.id
            {join}
            WHERE e.podcast_id = ?
            ORDER BY e.date DESC
        """,
            params + [podcast_id],
        )
        return self._fetchall_as_dicts(cursor)

    def get_all_episodes(self, username: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all episodes with podcast title (only a user's, if given)."""
        join, params = self._user_episode_join(username)
        cursor = self.conn.execute(
            f"""
            SELECT e.*, p.title as podcast_title
            FROM episodes e
            JOIN podcasts p ON e.podcast_id = p.id
            {join}
            ORDER BY e.date DESC
        """,
            params,
        )
        return self._fetchall_as_dicts(cursor)

    def _user_episode_join(self, username: Optional[str]):
        """SQL join restricting ``e`` (episodes) to a user's, and its params.
        No restriction when ``username`` is None."""
        if username is None:
            return "", []
        return (
            "JOIN user_episodes ue ON ue.episode_id = e.id AND ue.username = ?",
            [username],
        )

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
            self._decode_summary(row)
        return row

    def get_all_summaries(self, username: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all summaries (only a user's, if given)."""
        join, params = self._user_episode_join(username)
        cursor = self.conn.execute(
            f"""
            SELECT s.*, e.title as episode_title, p.title as podcast_title
            FROM summaries s
            JOIN episodes e ON s.episode_id = e.id
            JOIN podcasts p ON e.podcast_id = p.id
            {join}
            ORDER BY s.created_at DESC
        """,
            params,
        )
        rows = self._fetchall_as_dicts(cursor)
        for row in rows:
            self._decode_summary(row)
        return rows

    _SUMMARY_JSON_COLUMNS = (
        "key_topics",
        "themes",
        "quotes",
        "startups",
        "key_topics_fr",
        "themes_fr",
    )

    def _decode_summary(self, row: Dict[str, Any]):
        """Deserialize a summary row's JSON columns in place."""
        for col in self._SUMMARY_JSON_COLUMNS:
            if isinstance(row.get(col), str):
                row[col] = json.loads(row[col])

    # ------------------------------------------------------------------
    # Job tracking
    # ------------------------------------------------------------------

    def create_job(
        self,
        job_type: str,
        episode_id: Optional[int] = None,
        podcast_id: Optional[int] = None,
        run_id: Optional[str] = None,
        username: Optional[str] = None,
    ) -> str:
        """Create a new background job. Returns the job ID."""
        job_id = str(uuid.uuid4())
        self.conn.execute(
            """
            INSERT INTO jobs
                (id, episode_id, podcast_id, job_type, status, run_id, username)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
        """,
            (job_id, episode_id, podcast_id, job_type, run_id, username),
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

    def delete_jobs(self, job_ids: List[str]) -> None:
        """Delete jobs by id."""
        for job_id in job_ids:
            self.conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

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
                f"error = '{INTERRUPTED_ERROR}', "
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

    # A user sees the jobs they started and every job about a podcast they
    # subscribe to (the daily run's included).
    _JOB_VISIBLE = """
        (j.username = ? OR COALESCE(j.podcast_id, e.podcast_id) IN
            (SELECT podcast_id FROM user_podcasts WHERE username = ?))
    """

    def _jobs_where(self, condition: str, username: Optional[str]):
        """WHERE clause and params for jobs matching ``condition``, limited
        to those visible to ``username`` when given."""
        if username is None:
            return f" WHERE {condition}", []
        return f" WHERE {condition} AND {self._JOB_VISIBLE}", [username, username]

    def job_visible_to(self, job_id: str, username: str) -> bool:
        """True if the user started this job or follows its podcast."""
        where, params = self._jobs_where("j.id = ?", username)
        sql = "SELECT 1 FROM jobs j LEFT JOIN episodes e ON e.id = j.episode_id"
        return self.conn.execute(sql + where, [job_id] + params).fetchone() is not None

    def get_recent_jobs(
        self, limit: Optional[int] = 20, username: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get recent jobs ordered by creation time. limit=None (or <=0)
        returns all of them. Only a user's visible jobs, if given."""
        where, params = self._jobs_where("TRUE", username)
        sql = self._JOBS_SELECT + where + " ORDER BY j.created_at DESC"
        if limit is not None and limit > 0:
            cursor = self.conn.execute(sql + " LIMIT ?", params + [limit])
        else:
            cursor = self.conn.execute(sql, params)
        return self._fetchall_as_dicts(cursor)

    def get_active_jobs(self, username: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all pending or running jobs."""
        where, params = self._jobs_where("j.status IN ('pending', 'running')", username)
        cursor = self.conn.execute(
            self._JOBS_SELECT + where + " ORDER BY j.created_at", params
        )
        return self._fetchall_as_dicts(cursor)

    def get_failed_jobs(self, username: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all failed jobs, newest first."""
        where, params = self._jobs_where("j.status = 'failed'", username)
        cursor = self.conn.execute(
            self._JOBS_SELECT + where + " ORDER BY j.created_at DESC", params
        )
        return self._fetchall_as_dicts(cursor)

    def get_jobs_by_run(self, run_id: str) -> List[Dict[str, Any]]:
        """Get every job of one daily run, oldest first."""
        cursor = self.conn.execute(
            self._JOBS_SELECT + " WHERE j.run_id = ? ORDER BY j.created_at",
            (run_id,),
        )
        return self._fetchall_as_dicts(cursor)

    def now(self) -> datetime:
        """The database clock, as its CURRENT_TIMESTAMP defaults record it."""
        return self.conn.execute("SELECT CURRENT_TIMESTAMP::TIMESTAMP").fetchone()[0]

    def get_episodes_processed_outside_runs(
        self, since: Optional[datetime], username: str
    ) -> List[Dict[str, Any]]:
        """A user's episodes that a pipeline or digest job outside any daily
        run (backlog, or started by hand) finished after ``since`` (database
        clock; the last 24 hours if None), oldest first, with titles."""
        cursor = self.conn.execute(
            """
            SELECT e.id AS episode_id,
                   e.title AS episode_title,
                   p.title AS podcast_title,
                   MAX(j.completed_at) AS completed_at
            FROM jobs j
            JOIN episodes e ON e.id = j.episode_id
            JOIN podcasts p ON p.id = e.podcast_id
            JOIN user_episodes ue ON ue.episode_id = e.id AND ue.username = ?
            WHERE j.job_type IN ('full_pipeline', 'digest')
              AND j.status = 'completed'
              AND j.run_id IS NULL
              AND j.completed_at > COALESCE(
                  CAST(? AS TIMESTAMP), CURRENT_TIMESTAMP::TIMESTAMP - INTERVAL 1 DAY
              )
            GROUP BY e.id, e.title, p.title
            ORDER BY completed_at
        """,
            (username, since),
        )
        return self._fetchall_as_dicts(cursor)

    def get_episodes_added_since(
        self, since: datetime, username: str
    ) -> List[Dict[str, Any]]:
        """A user's episodes that entered their library at or after
        ``since`` (database clock), with podcast title."""
        cursor = self.conn.execute(
            """
            SELECT e.*, p.title as podcast_title
            FROM episodes e
            JOIN podcasts p ON e.podcast_id = p.id
            JOIN user_episodes ue ON ue.episode_id = e.id AND ue.username = ?
            WHERE ue.created_at >= ?
            ORDER BY e.date DESC
        """,
            (username, since),
        )
        return self._fetchall_as_dicts(cursor)

    def count_episodes_created_since(
        self, since: datetime, username: Optional[str] = None
    ) -> int:
        """Count episodes added to the library (a user's, if given) at or
        after ``since``."""
        if username is None:
            return self.conn.execute(
                "SELECT COUNT(*) FROM episodes WHERE created_at >= ?", (since,)
            ).fetchone()[0]
        return self.conn.execute(
            "SELECT COUNT(*) FROM user_episodes WHERE username = ? AND created_at >= ?",
            (username, since),
        ).fetchone()[0]

    def clear_finished_jobs(self, username: Optional[str] = None) -> int:
        """Delete completed and failed jobs (only a user's visible ones, if
        given). Pending/running jobs are kept."""
        return self._delete_jobs("j.status IN ('completed', 'failed')", username)

    def clear_failed_jobs(self, username: Optional[str] = None) -> int:
        """Delete only failed jobs (only a user's visible ones, if given)."""
        return self._delete_jobs("j.status = 'failed'", username)

    def _delete_jobs(self, condition: str, username: Optional[str]) -> int:
        where, params = self._jobs_where(condition, username)
        ids = [
            row[0]
            for row in self.conn.execute(
                "SELECT j.id FROM jobs j LEFT JOIN episodes e ON e.id = j.episode_id"
                + where,
                params,
            ).fetchall()
        ]
        for job_id in ids:
            self.conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return len(ids)

    # ------------------------------------------------------------------
    # Users, subscriptions and episode links
    # ------------------------------------------------------------------

    def ensure_user(
        self,
        username: str,
        display_name: Optional[str] = None,
        email: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return the user, creating them on first sign-in.

        The first user ever is an admin and takes over everything already in
        the library (from before users existed): every podcast and episode.
        """
        user = self.get_user(username)
        if user is None:
            first = self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
            self.conn.execute(
                "INSERT INTO users (username, display_name, email, role, settings) "
                "VALUES (?, ?, ?, ?, '{}')",
                (username, display_name, email, "admin" if first else "user"),
            )
            if first:
                self.conn.execute(
                    "INSERT INTO user_podcasts (username, podcast_id) "
                    "SELECT ?, id FROM podcasts",
                    (username,),
                )
                self.conn.execute(
                    "INSERT INTO user_episodes (username, episode_id) "
                    "SELECT ?, id FROM episodes",
                    (username,),
                )
            user = self.get_user(username)
        elif (display_name and display_name != user["display_name"]) or (
            email and email != user["email"]
        ):
            self.conn.execute(
                "UPDATE users SET display_name = COALESCE(?, display_name), "
                "email = COALESCE(?, email) WHERE username = ?",
                (display_name, email, username),
            )
            user = self.get_user(username)
        assert user is not None
        return user

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        """Get a user, with ``settings`` decoded to a dict."""
        row = self._fetchone_as_dict(
            self.conn.execute("SELECT * FROM users WHERE username = ?", (username,))
        )
        if row is not None:
            row["settings"] = json.loads(row["settings"] or "{}")
            row.pop("password_hash", None)
        return row

    def get_all_users(self) -> List[Dict[str, Any]]:
        """Get every user, oldest first, with ``settings`` decoded."""
        rows = self._fetchall_as_dicts(
            self.conn.execute("SELECT * FROM users ORDER BY created_at, username")
        )
        for row in rows:
            row["settings"] = json.loads(row["settings"] or "{}")
            row.pop("password_hash", None)
        return rows

    def count_users(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def get_password_hash(self, username: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT password_hash FROM users WHERE username = ?", (username,)
        ).fetchone()
        return row[0] if row else None

    def set_password_hash(self, username: str, password_hash: str):
        self.conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (password_hash, username),
        )

    def create_session(self, token_hash: str, username: str, expires_at: datetime):
        self.conn.execute(
            "INSERT INTO sessions (token_hash, username, expires_at) VALUES (?, ?, ?)",
            (token_hash, username, expires_at),
        )

    def get_session_username(self, token_hash: str, now: datetime) -> Optional[str]:
        """The user a session belongs to, or None if unknown or expired."""
        row = self.conn.execute(
            "SELECT username FROM sessions WHERE token_hash = ? AND expires_at > ?",
            (token_hash, now),
        ).fetchone()
        return row[0] if row else None

    def delete_session(self, token_hash: str):
        self.conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_user_sessions(self, username: str, keep: Optional[str] = None):
        """Sign a user out everywhere, except the session ``keep``."""
        self.conn.execute(
            "DELETE FROM sessions WHERE username = ? AND token_hash != ?",
            (username, keep or ""),
        )

    def get_owner(self) -> Optional[str]:
        """The first admin: the user the CLI and config-file feeds act as."""
        row = self.conn.execute(
            "SELECT username FROM users WHERE role = 'admin' "
            "ORDER BY created_at, username LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def set_user_role(self, username: str, role: str):
        """Make a user an 'admin' or a 'user'."""
        if role not in ("admin", "user"):
            raise ValueError(f"Unknown role: {role}")
        self.conn.execute(
            "UPDATE users SET role = ? WHERE username = ?", (role, username)
        )

    def update_user_settings(self, username: str, settings: Dict[str, Any]):
        """Replace a user's own settings."""
        self.conn.execute(
            "UPDATE users SET settings = ? WHERE username = ?",
            (json.dumps(settings), username),
        )

    def subscribe(self, username: str, podcast_id: int):
        """Add a podcast to a user's library."""
        self.conn.execute(
            "INSERT OR IGNORE INTO user_podcasts (username, podcast_id) VALUES (?, ?)",
            (username, podcast_id),
        )

    def unsubscribe(self, username: str, podcast_id: int) -> int:
        """Remove a podcast and its episodes from a user's library. Returns
        how many users still subscribe to it."""
        self.conn.execute(
            "DELETE FROM user_podcasts WHERE username = ? AND podcast_id = ?",
            (username, podcast_id),
        )
        self.conn.execute(
            "DELETE FROM user_episodes WHERE username = ? AND episode_id IN "
            "(SELECT id FROM episodes WHERE podcast_id = ?)",
            (username, podcast_id),
        )
        return self.conn.execute(
            "SELECT COUNT(*) FROM user_podcasts WHERE podcast_id = ?", (podcast_id,)
        ).fetchone()[0]

    def is_subscribed(self, username: str, podcast_id: int) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM user_podcasts WHERE username = ? AND podcast_id = ?",
                (username, podcast_id),
            ).fetchone()
            is not None
        )

    def get_episode_id_by_url(self, url: str) -> Optional[int]:
        row = self.conn.execute(
            "SELECT id FROM episodes WHERE url = ?", (url,)
        ).fetchone()
        return row[0] if row else None

    def link_episode(self, username: str, episode_id: int) -> bool:
        """Add an episode to a user's library, undoing an earlier delete.
        Returns True if it was not there already."""
        self.conn.execute(
            "DELETE FROM user_deleted_episodes WHERE username = ? AND url IN "
            "(SELECT url FROM episodes WHERE id = ?)",
            (username, episode_id),
        )
        if self.user_has_episode(username, episode_id):
            return False
        self.conn.execute(
            "INSERT INTO user_episodes (username, episode_id) VALUES (?, ?)",
            (username, episode_id),
        )
        return True

    def hide_episode(self, username: str, episode_id: int) -> int:
        """Delete an episode from a user's library, for good: their fetches
        skip it from now on (the URL is remembered), unless they hand-pick it.
        Once nobody has the episode it is erased with its transcript, summary,
        jobs and audio. Returns how many users still have it."""
        row = self.conn.execute(
            "SELECT e.url, e.title, p.title FROM episodes e "
            "LEFT JOIN podcasts p ON p.id = e.podcast_id WHERE e.id = ?",
            (episode_id,),
        ).fetchone()
        self.conn.execute(
            "DELETE FROM user_episodes WHERE username = ? AND episode_id = ?",
            (username, episode_id),
        )
        if row:
            self.conn.execute(
                "INSERT OR REPLACE INTO user_deleted_episodes "
                "(username, url, title, podcast_title, deleted_at) "
                "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (username, *row),
            )
        remaining = self.conn.execute(
            "SELECT COUNT(*) FROM user_episodes WHERE episode_id = ?", (episode_id,)
        ).fetchone()[0]
        if remaining == 0 and row:
            self.purge_episode(episode_id)
        return remaining

    def purge_episode(self, episode_id: int):
        """Erase an episode nobody has, with everything that hangs off it."""
        self.remove_audio(episode_id)
        for table in ("transcripts", "summaries", "jobs"):
            self.conn.execute(
                f"DELETE FROM {table} WHERE episode_id = ?", (episode_id,)
            )
        self.conn.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))

    def user_has_episode(self, username: str, episode_id: int) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM user_episodes WHERE username = ? AND episode_id = ?",
                (username, episode_id),
            ).fetchone()
            is not None
        )

    def get_deleted_episodes(self, username: str) -> List[Dict[str, Any]]:
        """The episodes a user deleted, newest deletion first (those from
        before dates were kept last)."""
        cursor = self.conn.execute(
            "SELECT url, title, podcast_title, deleted_at FROM user_deleted_episodes "
            "WHERE username = ? ORDER BY deleted_at DESC NULLS LAST, title",
            (username,),
        )
        return self._fetchall_as_dicts(cursor)

    def allow_deleted_episode(self, username: str, url: str) -> bool:
        """Forget that the user deleted this episode, so fetches may bring it
        back. Returns False if it was not on their list."""
        if not self.is_url_deleted(username, url):
            return False
        self.conn.execute(
            "DELETE FROM user_deleted_episodes WHERE username = ? AND url = ?",
            (username, url),
        )
        return True

    def is_url_deleted(self, username: str, url: str) -> bool:
        """True if the user deleted the episode with this URL from their library."""
        return (
            self.conn.execute(
                "SELECT 1 FROM user_deleted_episodes WHERE username = ? AND url = ?",
                (username, url),
            ).fetchone()
            is not None
        )

    # ------------------------------------------------------------------
    # App state
    # ------------------------------------------------------------------

    def get_state(self, key: str) -> Optional[str]:
        """Get a stored app-state value, or None if unset."""
        row = self.conn.execute(
            "SELECT value FROM app_state WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str):
        """Store an app-state value, replacing any previous one."""
        self.conn.execute(
            "INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)",
            (key, value),
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self, query: str, limit: int = 50, username: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Case-insensitive substring search over episode titles, summaries
        and transcript segments.

        Returns one dict per matching episode (episode fields plus
        ``podcast_title``), with ``title_match``, ``summary_match``,
        ``segment_count`` and ``segments`` (every matching transcript
        segment, in order). Episodes matching in the title come first, then
        the summary, then by number of matching segments, then newest.
        """
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"

        title_ids = {
            row[0]
            for row in self.conn.execute(
                "SELECT id FROM episodes WHERE title ILIKE ? ESCAPE '\\'", (pattern,)
            ).fetchall()
        }
        summary_rows = self._fetchall_as_dicts(
            self.conn.execute(
                """
                SELECT episode_id, full_summary, long_summary FROM summaries
                WHERE full_summary ILIKE ? ESCAPE '\\'
                   OR long_summary ILIKE ? ESCAPE '\\'
            """,
                (pattern, pattern),
            )
        )
        segment_rows = self._fetchall_as_dicts(
            self.conn.execute(
                """
                SELECT episode_id, timestamp_start, text FROM transcripts
                WHERE text ILIKE ? ESCAPE '\\'
                ORDER BY episode_id, timestamp_start
            """,
                (pattern,),
            )
        )

        summaries = {row["episode_id"]: row for row in summary_rows}
        segments: Dict[int, List[Dict[str, Any]]] = {}
        for row in segment_rows:
            segments.setdefault(row["episode_id"], []).append(
                {"start": row["timestamp_start"], "text": row["text"]}
            )

        ids = title_ids | set(summaries) | set(segments)
        if username is not None:
            ids &= {e["id"] for e in self.get_all_episodes(username)}
        if not ids:
            return []
        placeholders = ", ".join("?" for _ in ids)
        episodes = self._fetchall_as_dicts(
            self.conn.execute(
                f"""
                SELECT e.*, p.title as podcast_title
                FROM episodes e
                JOIN podcasts p ON e.podcast_id = p.id
                WHERE e.id IN ({placeholders})
            """,
                list(ids),
            )
        )

        results = []
        for ep in episodes:
            summary = summaries.get(ep["id"])
            ep["title_match"] = ep["id"] in title_ids
            ep["summary_match"] = summary is not None
            ep["summary_text"] = (
                (summary["full_summary"] or "")
                + "\n\n"
                + (summary["long_summary"] or "")
                if summary
                else None
            )
            ep["segments"] = segments.get(ep["id"], [])
            ep["segment_count"] = len(ep["segments"])
            results.append(ep)

        _oldest = datetime.min
        results.sort(key=lambda r: r["date"] or _oldest, reverse=True)
        results.sort(
            key=lambda r: (r["title_match"], r["summary_match"], r["segment_count"]),
            reverse=True,
        )
        return results[:limit]

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self, username: Optional[str] = None) -> Dict[str, int]:
        """Get pipeline statistics (a user's library, if given). Job counts
        are always for the whole queue, which every user shares."""
        stats = {}
        stats["total_podcasts"] = len(self.get_all_podcasts(username))
        join, params = self._user_episode_join(username)
        episodes = f"FROM episodes e {join}"
        stats["total_episodes"] = self.conn.execute(
            f"SELECT COUNT(*) {episodes}", params
        ).fetchone()[0]
        for status in ("downloaded", "transcribed", "processed"):
            stats[f"episodes_{status}"] = self.conn.execute(
                f"SELECT COUNT(*) {episodes} WHERE e.status = ?", params + [status]
            ).fetchone()[0]
        stats["total_summaries"] = self.conn.execute(
            f"SELECT COUNT(*) FROM summaries s JOIN episodes e ON e.id = s.episode_id"
            f" {join}",
            params,
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
