"""Episode listing, detail, and pipeline trigger routes.

Episodes are shared rows; each user sees only the episodes linked to them.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from p3.api.auth import current_username, require_admin
from p3.api.deps import get_db
from p3.api.job_queue import job_runner
from p3.api.models import EpisodeOut, SynopsisCreate
from p3.api.tasks import (
    queue_step_jobs,
    task_digest,
    task_full_pipeline,
    task_generate_synopsis,
    task_transcribe,
    task_translate,
)

router = APIRouter(prefix="/api/episodes", tags=["episodes"])


def user_episode(db, episode_id: int, username: str) -> dict:
    """The episode, or 404 if it isn't in the user's library."""
    episode = db.get_episode_by_id(episode_id)
    if not episode or not db.user_has_episode(username, episode_id):
        raise HTTPException(404, "Episode not found")
    return episode


@router.get("", response_model=list[EpisodeOut])
def list_episodes(
    podcast_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    username: str = Depends(current_username),
):
    db = get_db()
    if podcast_id:
        episodes = db.get_episodes_by_podcast(podcast_id, username)
    elif status:
        episodes = db.get_episodes_by_status(status, username)
    else:
        episodes = db.get_all_episodes(username)
    return episodes


@router.post("/process/{step}", response_model=dict)
def process_all_episodes(step: str, username: str = Depends(current_username)):
    """Queue a pipeline step (transcribe|digest|pipeline) for every eligible
    episode in the user's library. One job per episode; the runner executes
    them one at a time.

    Declared before ``/{episode_id}`` so ``process`` is not read as an id.
    """
    db = get_db()
    try:
        job_ids = queue_step_jobs(
            db, db.get_all_episodes(username), step, username=username
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"queued": len(job_ids), "job_ids": job_ids}


@router.post("/cleanup-audio", response_model=dict)
def cleanup_audio(_admin: dict = Depends(require_admin)):
    """Delete the audio of every processed episode, everyone's. Transcripts
    and summaries are kept; new digests already do this automatically."""
    return get_db().remove_processed_audio()


@router.post("/translate-all", response_model=dict)
def translate_all(_admin: dict = Depends(require_admin)):
    """Queue a French translation for every digest that has none yet."""
    db = get_db()
    job_ids = []
    for summary in db.get_all_summaries():
        if summary.get("full_summary_fr") or not summary.get("full_summary"):
            continue
        job_id = db.create_job("translate", episode_id=summary["episode_id"])
        job_runner.enqueue(task_translate, job_id, summary["episode_id"])
        job_ids.append(job_id)
    return {"queued": len(job_ids), "job_ids": job_ids}


@router.get("/{episode_id}", response_model=EpisodeOut)
def get_episode(episode_id: int, username: str = Depends(current_username)):
    return user_episode(get_db(), episode_id, username)


@router.delete("/{episode_id}")
def delete_episode(episode_id: int, username: str = Depends(current_username)):
    """Delete an episode from the user's library. Their fetches won't bring
    it back unless they hand-pick it again. Once nobody has it, its audio is
    deleted and it is no longer processed."""
    db = get_db()
    user_episode(db, episode_id, username)
    if db.hide_episode(username, episode_id) == 0:
        db.remove_audio(episode_id)
    return {"deleted": True}


@router.post("/{episode_id}/transcribe", response_model=dict)
def transcribe_episode(episode_id: int, username: str = Depends(current_username)):
    db = get_db()
    episode = user_episode(db, episode_id, username)
    if episode["status"] != "downloaded":
        raise HTTPException(
            400, f"Episode status is '{episode['status']}', expected 'downloaded'"
        )

    job_id = db.create_job(
        "transcribe",
        episode_id=episode_id,
        podcast_id=episode["podcast_id"],
        username=username,
    )
    job_runner.enqueue(task_transcribe, job_id, episode_id)
    return {"job_id": job_id}


@router.post("/{episode_id}/digest", response_model=dict)
def digest_episode(episode_id: int, username: str = Depends(current_username)):
    db = get_db()
    episode = user_episode(db, episode_id, username)
    if episode["status"] not in ("transcribed", "processed"):
        raise HTTPException(
            400,
            f"Episode status is '{episode['status']}', expected 'transcribed' or 'processed'",
        )

    job_id = db.create_job(
        "digest",
        episode_id=episode_id,
        podcast_id=episode["podcast_id"],
        username=username,
    )
    job_runner.enqueue(task_digest, job_id, episode_id)
    return {"job_id": job_id}


@router.post("/{episode_id}/translate", response_model=dict)
def translate_episode(episode_id: int, username: str = Depends(current_username)):
    """Add the Quebec French version of an older episode's digest."""
    db = get_db()
    episode = user_episode(db, episode_id, username)
    if not db.get_summary_by_episode(episode_id):
        raise HTTPException(400, "Episode has no summary yet; run digest first")

    job_id = db.create_job(
        "translate",
        episode_id=episode_id,
        podcast_id=episode["podcast_id"],
        username=username,
    )
    job_runner.enqueue(task_translate, job_id, episode_id)
    return {"job_id": job_id}


@router.post("/{episode_id}/synopsis", response_model=dict)
def generate_synopsis(
    episode_id: int,
    body: Optional[SynopsisCreate] = None,
    username: str = Depends(current_username),
):
    """Generate the long-form study-notes synopsis for one episode. This is
    an on-demand action; the digest step only produces the short summary."""
    db = get_db()
    episode = user_episode(db, episode_id, username)
    if not db.get_summary_by_episode(episode_id):
        raise HTTPException(400, "Episode has no summary yet; run digest first")

    job_id = db.create_job(
        "synopsis",
        episode_id=episode_id,
        podcast_id=episode["podcast_id"],
        username=username,
    )
    body = body or SynopsisCreate()
    job_runner.enqueue(
        task_generate_synopsis, job_id, episode_id, body.provider, body.model, username
    )
    return {"job_id": job_id}


@router.post("/{episode_id}/pipeline", response_model=dict)
def run_pipeline(episode_id: int, username: str = Depends(current_username)):
    """Run the full pipeline (transcribe → digest) on an episode."""
    db = get_db()
    episode = user_episode(db, episode_id, username)
    if episode["status"] == "processed":
        raise HTTPException(400, "Episode already fully processed")

    job_id = db.create_job(
        "full_pipeline",
        episode_id=episode_id,
        podcast_id=episode["podcast_id"],
        username=username,
    )
    job_runner.enqueue(task_full_pipeline, job_id, episode_id)
    return {"job_id": job_id}
