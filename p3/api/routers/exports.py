"""Export and download routes."""

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import FileResponse

from p3.api.deps import get_db
from p3.api.tasks import task_export

router = APIRouter(prefix="/api/exports", tags=["exports"])

EXPORT_DIR = Path("exports")


@router.get("/{date_str}")
def download_export(date_str: str, format: str = Query("markdown")):
    """Download an existing export file."""
    ext = "md" if format == "markdown" else "json"
    path = EXPORT_DIR / f"digest_{date_str}.{ext}"
    if not path.exists():
        raise HTTPException(404, f"Export not found: {path.name}")
    media = "text/markdown" if ext == "md" else "application/json"
    return FileResponse(path, media_type=media, filename=path.name)


@router.post("", response_model=dict)
def generate_export(
    date: str = Query(None, description="YYYY-MM-DD, defaults to today"),
    formats: str = Query("markdown,json", description="Comma-separated formats"),
    background_tasks: BackgroundTasks = None,
):
    """Generate export files for a given date."""
    db = get_db()
    target_date = date or datetime.now().strftime("%Y-%m-%d")
    fmt_list = [f.strip() for f in formats.split(",")]

    job_id = db.create_job("export")
    background_tasks.add_task(task_export, job_id, target_date, fmt_list)
    return {"job_id": job_id}
