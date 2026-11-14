"""Transcripts and summaries."""

import json
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from .library import Library


class Digests(Library):
    def add_transcript_segments(self, episode_id: int, segments: List[Dict[str, Any]]):
        """Replace an episode's transcript segments with a fresh set, so
        transcribing an episode again never duplicates them. All or nothing:
        an interruption leaves the previous transcript in place."""
        with self.transaction():
            self.conn.execute(
                "DELETE FROM transcripts WHERE episode_id = ?", (episode_id,)
            )
            # A new transcript's speaker labels needn't mean the same people.
            self.conn.execute(
                "DELETE FROM episode_speakers WHERE episode_id = ?", (episode_id,)
            )
            if segments:
                self.conn.executemany(
                    """
                    INSERT INTO transcripts
                        (episode_id, speaker, timestamp_start, timestamp_end,
                         text, confidence)
                    VALUES (?, ?, ?, ?, ?, ?)
                """,
                    [
                        (
                            episode_id,
                            segment.get("speaker"),
                            segment.get("start"),
                            segment.get("end"),
                            segment.get("text"),
                            segment.get("confidence"),
                        )
                        for segment in segments
                    ],
                )

    def store_transcript(
        self, episode_id: int, segments: List[Dict[str, Any]], language: Optional[str]
    ):
        """Store a finished transcription: the segments, the language
        detected, and the episode's new status, together."""
        with self.transaction():
            self.add_transcript_segments(episode_id, segments)
            self.set_episode_language(episode_id, language)
            self.update_episode_status(episode_id, "transcribed")

    def replace_episode_speakers(
        self, episode_id: int, speakers: Dict[str, Dict[str, Any]]
    ):
        """Replace who an episode's speaker labels are: {label: {name, role,
        seconds}} (see p3/speakers.py)."""
        with self.transaction():
            self.conn.execute(
                "DELETE FROM episode_speakers WHERE episode_id = ?", (episode_id,)
            )
            if speakers:
                self.conn.executemany(
                    "INSERT INTO episode_speakers (episode_id, label, name, role, "
                    "seconds) VALUES (?, ?, ?, ?, ?)",
                    [
                        (
                            episode_id,
                            label,
                            s.get("name"),
                            s.get("role"),
                            s.get("seconds"),
                        )
                        for label, s in speakers.items()
                    ],
                )

    def get_episode_speakers(self, episode_id: int) -> Dict[str, Dict[str, Any]]:
        """{label: {name, role, seconds}} for an episode, most talk first; {}
        if its speakers weren't named."""
        cursor = self.conn.execute(
            "SELECT label, name, role, seconds FROM episode_speakers "
            "WHERE episode_id = ? ORDER BY seconds DESC, label",
            (episode_id,),
        )
        return {
            row["label"]: {k: row[k] for k in ("name", "role", "seconds")}
            for row in self._fetchall_as_dicts(cursor)
        }

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

        with self.transaction():
            self.conn.execute(
                "DELETE FROM summaries WHERE episode_id = ?", (episode_id,)
            )
            self.conn.execute(
                """
                INSERT INTO summaries
                    (episode_id, key_topics, themes, quotes, startups,
                     full_summary, digest_date, long_summary, long_summary_fr)
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

    def get_summary_by_episode(self, episode_id: int) -> Optional[Dict[str, Any]]:
        """Get summary for a specific episode."""
        cursor = self.conn.execute(
            """
            SELECT s.*, e.title as episode_title, p.title as podcast_title,
                e.language as episode_language
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

    def get_summaries_by_date(
        self, date: datetime, username: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get all summaries for a specific date (only a user's, if given)."""
        join, params = self._user_episode_join(username)
        cursor = self.conn.execute(
            f"""
            SELECT s.*, e.title as episode_title, p.title as podcast_title,
                e.language as episode_language
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

    def get_all_summaries(self, username: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all summaries (only a user's, if given)."""
        join, params = self._user_episode_join(username)
        cursor = self.conn.execute(
            f"""
            SELECT s.*, e.title as episode_title, p.title as podcast_title,
                e.language as episode_language
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
