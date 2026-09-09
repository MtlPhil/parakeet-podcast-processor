"""Episode listing, detail, and pipeline trigger routes."""

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from typing import Optional

from p3.api.deps import get_db
from p3.api.models import EpisodeOut
from p3.api.tasks import task_transcribe, task_digest, task_full_pipeline

router = APIRouter(prefix="/api/episodes", tags=["episodes"])


@router.get("", response_model=list[EpisodeOut])
def list_episodes(
    podcast_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
):
    db = get_db()
    if podcast_id:
        episodes = db.get_episodes_by_podcast(podcast_id)
    elif status:
        episodes = db.get_episodes_by_status(status)
    else:
        episodes = db.get_all_episodes()
    return episodes


@router.get("/{episode_id}", response_model=EpisodeOut)
def get_episode(episode_id: int):
    db = get_db()
    episode = db.get_episode_by_id(episode_id)
    if not episode:
        raise HTTPException(404, "Episode not found")
    return episode


@router.post("/{episode_id}/transcribe", response_model=dict)
def transcribe_episode(episode_id: int, background_tasks: BackgroundTasks):
    db = get_db()
    episode = db.get_episode_by_id(episode_id)
    if not episode:
        raise HTTPException(404, "Episode not found")
    if episode["status"] != "downloaded":
        raise HTTPException(400, f"Episode status is '{episode['status']}', expected 'downloaded'")

    job_id = db.create_job("transcribe", episode_id=episode_id)
    background_tasks.add_task(task_transcribe, job_id, episode_id)
    return {"job_id": job_id}


@router.post("/{episode_id}/digest", response_model=dict)
def digest_episode(episode_id: int, background_tasks: BackgroundTasks):
    db = get_db()
    episode = db.get_episode_by_id(episode_id)
    if not episode:
        raise HTTPException(404, "Episode not found")
    if episode["status"] != "transcribed":
        raise HTTPException(400, f"Episode status is '{episode['status']}', expected 'transcribed'")

    job_id = db.create_job("digest", episode_id=episode_id)
    background_tasks.add_task(task_digest, job_id, episode_id)
    return {"job_id": job_id}


@router.post("/{episode_id}/pipeline", response_model=dict)
def run_pipeline(episode_id: int, background_tasks: BackgroundTasks):
    """Run the full pipeline (transcribe → digest) on an episode."""
    db = get_db()
    episode = db.get_episode_by_id(episode_id)
    if not episode:
        raise HTTPException(404, "Episode not found")
    if episode["status"] == "processed":
        raise HTTPException(400, "Episode already fully processed")

    job_id = db.create_job("full_pipeline", episode_id=episode_id)
    background_tasks.add_task(task_full_pipeline, job_id, episode_id)
    return {"job_id": job_id}
