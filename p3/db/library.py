"""Podcasts and episodes."""

import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .schema import Schema


def _has_notes(description: Optional[str]) -> bool:
    """Whether show notes hold any text once markup is removed (blank ones are
    not stored, so they can't keep an episode waiting to be indexed)."""
    from p3.chunker import notes_text

    return bool(notes_text(description))


class Library(Schema):
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

    def get_podcast_by_id(self, podcast_id: int) -> Optional[Dict[str, Any]]:
        """Get podcast by ID."""
        cursor = self.conn.execute("SELECT * FROM podcasts WHERE id = ?", (podcast_id,))
        return self._fetchone_as_dict(cursor)

    def get_all_podcasts(self, username: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all podcasts, or only those a user subscribes to, then with
        ``followed_at``, when they subscribed."""
        if username is None:
            cursor = self.conn.execute(
                "SELECT * FROM podcasts ORDER BY created_at DESC"
            )
        else:
            cursor = self.conn.execute(
                """
                SELECT p.*, up.created_at AS followed_at FROM podcasts p
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
            "DELETE FROM user_deleted_episodes WHERE podcast_id = ? OR url IN "
            "(SELECT url FROM episodes WHERE podcast_id = ?)",
            (podcast_id, podcast_id),
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

    def add_episode(
        self,
        podcast_id: int,
        title: str,
        date: Optional[datetime],
        url: str,
        file_path: Optional[str] = None,
        description: Optional[str] = None,
        guid: Optional[str] = None,
    ) -> int:
        """Add new episode. ``description`` is its show notes, ``guid`` the
        feed's id for it.

        ``date`` is stored as naive UTC. The ``date`` column is DuckDB's
        timezone-naive TIMESTAMP, so binding a timezone-aware datetime (as
        RSS and YouTube publication dates are) silently converts it to the
        host machine's local wall-clock time instead -- making the stored
        value depend on whatever timezone P3 happens to run in.
        """
        if date is not None and date.tzinfo is not None:
            date = date.astimezone(timezone.utc).replace(tzinfo=None)
        if not _has_notes(description):
            description = None
        next_id = self.conn.execute("SELECT nextval('episode_id_seq')").fetchone()[0]
        self.conn.execute(
            """
            INSERT INTO episodes (id, podcast_id, title, date, url, file_path,
                description, guid)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                next_id,
                podcast_id,
                title,
                date,
                url,
                file_path,
                description or None,
                guid or None,
            ),
        )
        return next_id

    def get_episode_url_by_guid(
        self, podcast_id: int, guid: Optional[str]
    ) -> Optional[str]:
        """The URL a podcast's episode with this feed ``guid`` is stored
        under, which may be older than the one its feed lists now."""
        if not guid:
            return None
        row = self.conn.execute(
            "SELECT url FROM episodes WHERE podcast_id = ? AND guid = ?",
            (podcast_id, guid),
        ).fetchone()
        return row[0] if row else None

    def set_missing_guids(self, podcast_id: int, guids: Dict[str, str]) -> int:
        """Store the feed ``guid`` of a podcast's episodes stored without one
        (added before guids were kept), from ``guids``: audio URL -> guid.
        Returns how many changed."""
        rows = self.conn.execute(
            "SELECT id, url FROM episodes WHERE podcast_id = ? AND guid IS NULL",
            (podcast_id,),
        ).fetchall()
        changed = 0
        for episode_id, url in rows:
            if guids.get(url):
                self.conn.execute(
                    "UPDATE episodes SET guid = ? WHERE id = ?",
                    (guids[url], episode_id),
                )
                changed += 1
        return changed

    def set_missing_description(self, url: str, description: Optional[str]) -> bool:
        """Store the show notes of an episode that has none yet (episodes
        added before they were kept). True if it changed."""
        if not _has_notes(description):
            return False
        row = self.conn.execute(
            "SELECT id FROM episodes WHERE url = ? "
            "AND (description IS NULL OR description = '')",
            (url,),
        ).fetchone()
        if not row:
            return False
        # No RETURNING: DuckDB runs an UPDATE ... RETURNING as delete +
        # reinsert, which the foreign keys from transcripts reject.
        self.conn.execute(
            "UPDATE episodes SET description = ? WHERE id = ?", (description, row[0])
        )
        return True

    def get_episodes_missing_description(self) -> List[Dict[str, Any]]:
        """Episodes without show notes, with their podcast's feed URL and
        source type."""
        cursor = self.conn.execute("""
            SELECT e.id, e.podcast_id, e.title, e.url,
                p.rss_url, p.source_type
            FROM episodes e
            JOIN podcasts p ON e.podcast_id = p.id
            WHERE e.description IS NULL OR e.description = ''
            ORDER BY e.podcast_id, e.id
        """)
        return self._fetchall_as_dicts(cursor)

    def set_episode_description(self, episode_id: int, description: str):
        """Store an episode's show notes, unless they hold no text."""
        if not _has_notes(description):
            return
        self.conn.execute(
            "UPDATE episodes SET description = ? WHERE id = ?",
            (description, episode_id),
        )

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

    def get_episode_id_by_url(self, url: str) -> Optional[int]:
        row = self.conn.execute(
            "SELECT id FROM episodes WHERE url = ?", (url,)
        ).fetchone()
        return row[0] if row else None

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

    # Held while an episode's Ask passages are rewritten (see
    # Ask.replace_chunks) or the episode is erased, so neither sees the other
    # half done.
    _chunk_lock = threading.Lock()

    def purge_episode(self, episode_id: int):
        """Erase an episode nobody has, with everything that hangs off it,
        after any rewrite of its Ask passages finishes. (Not one transaction:
        DuckDB's foreign-key check doesn't see rows deleted earlier in the
        same transaction, so the episode row couldn't go.)"""
        self.remove_audio(episode_id)
        with self._chunk_lock:
            for table in ("transcripts", "summaries", "jobs", "chunk_terms", "chunks"):
                self.conn.execute(
                    f"DELETE FROM {table} WHERE episode_id = ?", (episode_id,)
                )
            self.conn.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
