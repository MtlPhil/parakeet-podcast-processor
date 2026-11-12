"""Creating the schema and bringing an older database up to date.

The schema changes through numbered migrations. Each runs once per database,
in order, and is then recorded in ``schema_migrations``. To change the
schema, add a method and append it to ``Schema._MIGRATIONS``; never edit one
that has shipped.
"""

import logging
from typing import Optional

from .connection import Connection

logger = logging.getLogger(__name__)


class Schema(Connection):
    def __init__(self, db_path: Optional[str] = None):
        super().__init__(db_path)
        try:
            self._migrate()
            self._resync_sequences()
        except BaseException:
            self.close()
            raise

    def _migrate(self):
        """Run the migrations this database has not had yet."""
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, "
            "applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        applied = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
        if applied > len(self._MIGRATIONS):
            raise RuntimeError(
                f"{self.db_path} was last opened by a newer version of P3 "
                f"(schema {applied}; this version knows {len(self._MIGRATIONS)})"
            )
        for version, migration in enumerate(self._MIGRATIONS, start=1):
            if version > applied:
                logger.info("Database migration %d: %s", version, migration.__name__)
                migration(self)
                self.conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)", (version,)
                )

    def _baseline(self):
        """Migration 1: the schema as of November 2026. Every statement is
        safe to repeat, because it also adopts databases made before
        migrations were numbered, at whatever point they had reached."""
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
        # Migration: the show notes (the feed's episode description, or the
        # YouTube video's), used to name and spell hosts and guests. Filled in
        # by later fetches for episodes added before.
        self.conn.execute(
            "ALTER TABLE episodes ADD COLUMN IF NOT EXISTS description VARCHAR"
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
        # Migration: what a fetch job fetches (a hand-picked selection, as a
        # JSON list of guids, and/or an episode limit), so a retry repeats it
        self.conn.execute(
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS episode_guids VARCHAR"
        )
        self.conn.execute(
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS max_episodes INTEGER"
        )
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

    _MIGRATIONS = (_baseline,)

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
