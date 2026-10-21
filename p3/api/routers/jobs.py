"""Job status routes."""

from fastapi import APIRouter, Depends, HTTPException

from p3.api.auth import current_username
from p3.api.deps import get_db
from p3.api.job_queue import job_runner
from p3.api.models import JobOut
from p3.api.tasks import (
    resume_daily_run,
    task_digest,
    task_fetch,
    task_full_pipeline,
    task_transcribe,
)

from p3.database import INTERRUPTED_ERROR

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

# Job types whose work can be reconstructed from the job row alone.
_EPISODE_TASKS = {
    "transcribe": task_transcribe,
    "digest": task_digest,
    "full_pipeline": task_full_pipeline,
}

# The episode status each step still needs in order to be worth retrying.
_STEP_STILL_NEEDED = {
    "transcribe": lambda st: st == "downloaded",
    "digest": lambda st: st == "transcribed",
    "full_pipeline": lambda st: st != "processed",
}


def _newer_completed_fetch(db, podcast_id, after) -> bool:
    """True if a later fetch for this podcast has already completed."""
    for j in db.get_recent_jobs(limit=200):
        if (
            j["job_type"] == "fetch"
            and j["podcast_id"] == podcast_id
            and j["status"] == "completed"
            and j["created_at"]
            and after
            and j["created_at"] > after
        ):
            return True
    return False


def _retry_plan(db, job):
    """Return (task_fn, task_args, create_kwargs) to re-run this failed job,
    or None if it is unsupported, its target is gone, or it already succeeded
    on a later attempt."""
    job_type = job["job_type"]

    if job_type == "fetch":
        pid = job["podcast_id"]
        if not pid or not db.get_podcast_by_id(pid):
            return None
        if _newer_completed_fetch(db, pid, job["created_at"]):
            return None
        return (
            task_fetch,
            (pid, None, None, job["username"]),
            {"podcast_id": pid, "username": job["username"]},
        )

    if job_type in _EPISODE_TASKS:
        episode = db.get_episode_by_id(job["episode_id"]) if job["episode_id"] else None
        if not episode or not _STEP_STILL_NEEDED[job_type](episode["status"]):
            return None
        return (
            _EPISODE_TASKS[job_type],
            (episode["id"],),
            {
                "episode_id": episode["id"],
                "podcast_id": episode["podcast_id"],
                "username": job["username"],
            },
        )

    return None


@router.get("", response_model=list[JobOut])
def list_jobs(
    active_only: bool = False,
    limit: int | None = None,
    username: str = Depends(current_username),
):
    """List the jobs the user started or that concern their podcasts. limit
    is omitted (or <=0) to return all — the frontend filters and paginates
    client-side."""
    db = get_db()
    if active_only:
        return db.get_active_jobs(username)
    return db.get_recent_jobs(limit=limit, username=username)


@router.post("/retry-failed", response_model=dict)
def retry_failed_jobs(username: str = Depends(current_username)):
    """Re-queue every failed job that still needs doing, then clear the failed
    records — they're superseded by the new jobs (or already resolved).
    Each target+step is retried at most once. Declared before ``/{job_id}``
    so ``retry-failed`` is not read as an id."""
    db = get_db()
    seen = set()
    job_ids = []
    # A daily run a restart cut short is resumed as a whole, so it still
    # finishes and emails its report; plain re-queued jobs would not.
    resumed = resume_daily_run(db, username)
    for job in db.get_failed_jobs(username):
        key = (job["job_type"], job["episode_id"], job["podcast_id"])
        if key in seen:
            continue
        seen.add(key)
        plan = _retry_plan(db, job)
        if plan is None:
            continue
        task_fn, task_args, create_kwargs = plan
        new_id = db.create_job(job["job_type"], **create_kwargs)
        job_runner.enqueue(task_fn, new_id, *task_args)
        job_ids.append(new_id)
    db.clear_failed_jobs(username)
    return {
        "queued": len(job_ids) + (1 if resumed else 0),
        "job_ids": job_ids,
        "resumed_run": resumed,
    }


@router.delete("/failed", response_model=dict)
def clear_failed_jobs(username: str = Depends(current_username)):
    """Delete the user's visible failed job records. Declared before
    ``/{job_id}`` so ``failed`` is not read as an id."""
    db = get_db()
    return {"deleted": db.clear_failed_jobs(username)}


@router.delete("", response_model=dict)
def clear_jobs(username: str = Depends(current_username)):
    """Delete the user's visible completed and failed job records.
    Pending/running jobs are kept."""
    db = get_db()
    return {"deleted": db.clear_finished_jobs(username)}


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: str, username: str = Depends(current_username)):
    db = get_db()
    job = db.get_job(job_id)
    if not job or not db.job_visible_to(job_id, username):
        raise HTTPException(404, "Job not found")
    return job


@router.post("/{job_id}/retry", response_model=dict)
def retry_job(job_id: str, username: str = Depends(current_username)):
    """Re-queue a single failed job as a new job. The original row is kept as
    history."""
    db = get_db()
    job = db.get_job(job_id)
    if not job or not db.job_visible_to(job_id, username):
        raise HTTPException(404, "Job not found")
    if job["status"] != "failed":
        raise HTTPException(400, "Only failed jobs can be retried")

    if job["run_id"] and job["error"] == INTERRUPTED_ERROR:
        # Cut short with its daily run: resume the run so it still finishes
        # and emails its report, rather than re-running this job alone.
        if resume_daily_run(db, username) == job["run_id"]:
            return {"job_id": None, "resumed_run": job["run_id"]}

    plan = _retry_plan(db, job)
    if plan is None:
        raise HTTPException(
            400,
            "This job can't be retried — unsupported type, its target is gone, "
            "or it already succeeded on a later attempt",
        )
    task_fn, task_args, create_kwargs = plan
    new_id = db.create_job(job["job_type"], **create_kwargs)
    job_runner.enqueue(task_fn, new_id, *task_args)
    return {"job_id": new_id}
