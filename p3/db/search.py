"""Search and statistics."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from .users import Users


class Search(Users):
    def search(
        self, query: str, limit: int = 50, username: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Case-insensitive substring search over episode titles, summaries
        (English and French) and transcript segments.

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
        summary_columns = (
            "full_summary",
            "long_summary",
            "full_summary_fr",
            "long_summary_fr",
        )
        summary_rows = self._fetchall_as_dicts(
            self.conn.execute(
                f"SELECT episode_id, {', '.join(summary_columns)} FROM summaries "
                "WHERE "
                + " OR ".join(f"{c} ILIKE ? ESCAPE '\\'" for c in summary_columns),
                [pattern] * len(summary_columns),
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
                "\n\n".join(summary[c] for c in summary_columns if summary[c])
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
