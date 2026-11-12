"""Background jobs."""

import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .digests import Digests

# Error recorded on jobs a server restart cut short (see fail_stale_jobs).
INTERRUPTED_ERROR = "interrupted: server restarted"


class Jobs(Digests):
    def create_job(
        self,
        job_type: str,
        episode_id: Optional[int] = None,
        podcast_id: Optional[int] = None,
        run_id: Optional[str] = None,
        username: Optional[str] = None,
        episode_guids: Optional[List[str]] = None,
        max_episodes: Optional[int] = None,
    ) -> str:
        """Create a new background job. Returns the job ID. A fetch job
        records what it fetches: ``episode_guids`` (hand-picked) and/or
        ``max_episodes``, read back by ``job_fetch_args``."""
        job_id = str(uuid.uuid4())
        guids = json.dumps(episode_guids) if episode_guids is not None else None
        self.conn.execute(
            """
            INSERT INTO jobs
                (id, episode_id, podcast_id, job_type, status, run_id, username,
                 episode_guids, max_episodes)
            VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)
        """,
            (
                job_id,
                episode_id,
                podcast_id,
                job_type,
                run_id,
                username,
                guids,
                max_episodes,
            ),
        )
        return job_id

    @staticmethod
    def job_fetch_args(job: Dict[str, Any]) -> Tuple[Optional[int], Optional[List]]:
        """(max_episodes, episode_guids) a fetch job was created with."""
        raw = job.get("episode_guids")
        return job.get("max_episodes"), json.loads(raw) if raw is not None else None

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

    def failed_in_a_row(self) -> Dict[int, int]:
        """For each episode, how many of its transcribe, digest and pipeline
        jobs failed since the last one that completed. Failures a server
        restart caused (INTERRUPTED_ERROR) are not the episode's and don't
        count."""
        rows = self.conn.execute(
            """
            SELECT j.episode_id, COUNT(*) FROM jobs j
            WHERE j.job_type IN ('transcribe', 'digest', 'full_pipeline')
              AND j.status = 'failed'
              AND j.error IS DISTINCT FROM ?
              AND j.created_at > COALESCE((
                  SELECT MAX(c.created_at) FROM jobs c
                  WHERE c.episode_id = j.episode_id
                    AND c.status = 'completed'
                    AND c.job_type IN ('transcribe', 'digest', 'full_pipeline')
              ), TIMESTAMP '1970-01-01')
            GROUP BY j.episode_id
        """,
            [INTERRUPTED_ERROR],
        ).fetchall()
        return {episode_id: count for episode_id, count in rows}

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
