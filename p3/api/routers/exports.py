"""Export and download routes."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from p3.api.auth import current_username
from p3.api.deps import content_dir, get_db
from p3.api.job_queue import job_runner
from p3.api.tasks import task_export

router = APIRouter(prefix="/api/exports", tags=["exports"])


@router.get("/{date_str}")
def download_export(
    date_str: str,
    format: str = Query("markdown"),
    username: str = Depends(current_username),
):
    """Download one of the user's export files."""
    ext = "md" if format == "markdown" else "json"
    if not date_str.replace("-", "").isdigit():
        raise HTTPException(400, "date must be YYYY-MM-DD")
    path = content_dir("exports", username) / f"digest_{date_str}.{ext}"
    if not path.exists():
        raise HTTPException(404, f"Export not found: {path.name}")
    media = "text/markdown" if ext == "md" else "application/json"
    return FileResponse(path, media_type=media, filename=path.name)


@router.post("", response_model=dict)
def generate_export(
    date: str = Query(None, description="YYYY-MM-DD, defaults to today"),
    formats: str = Query("markdown,json", description="Comma-separated formats"),
    username: str = Depends(current_username),
):
    """Generate export files of the user's digests for a date, in their
    language."""
    db = get_db()
    target_date = date or datetime.now().strftime("%Y-%m-%d")
    fmt_list = [f.strip() for f in formats.split(",")]

    job_id = db.create_job("export", username=username)
    job_runner.enqueue(task_export, job_id, target_date, fmt_list, username)
    return {"job_id": job_id}
