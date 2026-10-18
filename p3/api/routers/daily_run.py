"""Summary of the last scheduled daily run."""

from typing import Optional

from fastapi import APIRouter, Depends

from p3.api import tasks
from p3.api.auth import current_username
from p3.api.deps import get_db
from p3.api.models import DailyRunOut

router = APIRouter(prefix="/api/daily-run", tags=["daily-run"])


@router.get("", response_model=Optional[DailyRunOut])
def get_last_daily_run(username: str = Depends(current_username)):
    """The user's last daily run: progress and results, or null if they
    have not had one."""
    db = get_db()
    run_id = db.get_state(tasks.run_key("id", username))
    if not run_id:
        return None

    jobs = db.get_jobs_by_run(run_id)
    if db.get_state(tasks.run_key("finished_id", username)) == run_id:
        state = "finished"
    elif run_id in tasks.active_run_ids:
        state = "running"
    else:
        state = "interrupted"

    counts: dict[str, dict[str, int]] = {}
    for job in jobs:
        by_status = counts.setdefault(job["job_type"], {})
        by_status[job["status"]] = by_status.get(job["status"], 0) + 1

    created = [j["created_at"] for j in jobs if j["created_at"]]
    export_message = next(
        (
            j["message"]
            for j in jobs
            if j["job_type"] == "export" and j["status"] == "completed"
        ),
        None,
    )
    return {
        "run_id": run_id,
        "started_at": db.get_state(tasks.run_key("started_at", username)),
        "state": state,
        "new_episodes": (
            db.count_episodes_created_since(min(created), username) if created else 0
        ),
        "jobs": counts,
        "failures": [
            {
                "job_id": j["id"],
                "job_type": j["job_type"],
                "podcast_title": j["podcast_title"],
                "episode_title": j["episode_title"],
                "status": j["status"],
                "error": j["error"],
            }
            for j in jobs
            # A completed job can still carry problems (e.g. a feed that
            # could not be read); they are reported without failing the run.
            if j["status"] == "failed" or j["error"]
        ],
        "export_message": export_message,
    }
