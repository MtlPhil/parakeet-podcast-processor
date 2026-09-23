"""Episode listing, detail, and pipeline trigger routes."""

from fastapi import APIRouter, HTTPException, Query
from typing import Optional

from p3.api.deps import get_db
from p3.api.job_queue import job_runner
from p3.api.models import EpisodeOut, SynopsisCreate
from p3.api.tasks import (
    queue_step_jobs,
    task_digest,
    task_full_pipeline,
    task_generate_synopsis,
    task_transcribe,
)

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


@router.post("/process/{step}", response_model=dict)
def process_all_episodes(step: str):
    """Queue a pipeline step (transcribe|digest|pipeline) for every eligible
    episode in the library. One job per episode; the runner executes them one
    at a time.

    Declared before ``/{episode_id}`` so ``process`` is not read as an id.
    """
    db = get_db()
    try:
        job_ids = queue_step_jobs(db, db.get_all_episodes(), step)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"queued": len(job_ids), "job_ids": job_ids}


@router.get("/{episode_id}", response_model=EpisodeOut)
def get_episode(episode_id: int):
    db = get_db()
    episode = db.get_episode_by_id(episode_id)
    if not episode:
        raise HTTPException(404, "Episode not found")
    return episode


@router.post("/{episode_id}/transcribe", response_model=dict)
def transcribe_episode(episode_id: int):
    db = get_db()
    episode = db.get_episode_by_id(episode_id)
    if not episode:
        raise HTTPException(404, "Episode not found")
    if episode["status"] != "downloaded":
        raise HTTPException(400, f"Episode status is '{episode['status']}', expected 'downloaded'")

    job_id = db.create_job(
        "transcribe", episode_id=episode_id, podcast_id=episode["podcast_id"]
    )
    job_runner.enqueue(task_transcribe, job_id, episode_id)
    return {"job_id": job_id}


@router.post("/{episode_id}/digest", response_model=dict)
def digest_episode(episode_id: int):
    db = get_db()
    episode = db.get_episode_by_id(episode_id)
    if not episode:
        raise HTTPException(404, "Episode not found")
    if episode["status"] not in ("transcribed", "processed"):
        raise HTTPException(400, f"Episode status is '{episode['status']}', expected 'transcribed' or 'processed'")

    job_id = db.create_job(
        "digest", episode_id=episode_id, podcast_id=episode["podcast_id"]
    )
    job_runner.enqueue(task_digest, job_id, episode_id)
    return {"job_id": job_id}


@router.post("/{episode_id}/synopsis", response_model=dict)
def generate_synopsis(episode_id: int, body: Optional[SynopsisCreate] = None):
    """Generate the long-form study-notes synopsis for one episode. This is
    an on-demand action; the digest step only produces the short summary."""
    db = get_db()
    episode = db.get_episode_by_id(episode_id)
    if not episode:
        raise HTTPException(404, "Episode not found")
    if not db.get_summary_by_episode(episode_id):
        raise HTTPException(400, "Episode has no summary yet; run digest first")

    job_id = db.create_job(
        "synopsis", episode_id=episode_id, podcast_id=episode["podcast_id"]
    )
    body = body or SynopsisCreate()
    job_runner.enqueue(task_generate_synopsis, job_id, episode_id, body.provider, body.model)
    return {"job_id": job_id}


@router.post("/{episode_id}/pipeline", response_model=dict)
def run_pipeline(episode_id: int):
    """Run the full pipeline (transcribe → digest) on an episode."""
    db = get_db()
    episode = db.get_episode_by_id(episode_id)
    if not episode:
        raise HTTPException(404, "Episode not found")
    if episode["status"] == "processed":
        raise HTTPException(400, "Episode already fully processed")

    job_id = db.create_job(
        "full_pipeline", episode_id=episode_id, podcast_id=episode["podcast_id"]
    )
    job_runner.enqueue(task_full_pipeline, job_id, episode_id)
    return {"job_id": job_id}
