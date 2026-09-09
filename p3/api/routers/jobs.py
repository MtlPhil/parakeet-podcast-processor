"""Job status routes."""

from fastapi import APIRouter, HTTPException

from p3.api.deps import get_db
from p3.api.models import JobOut

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("", response_model=list[JobOut])
def list_jobs(active_only: bool = False):
    db = get_db()
    if active_only:
        return db.get_active_jobs()
    return db.get_recent_jobs(limit=50)


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: str):
    db = get_db()
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job
