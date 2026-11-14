"""Transcript viewing routes."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from p3.api.auth import current_username, require_admin
from p3.api.deps import attachment, get_db
from p3.api.job_queue import job_runner
from p3.api.models import SpeakerList, SpeakerRename, TranscriptSegment
from p3.api.routers.episodes import user_episode
from p3.exporter import DigestExporter

router = APIRouter(prefix="/api/episodes", tags=["transcripts"])


@router.post("/transcripts/dedupe", response_model=dict)
def dedupe_transcripts(_admin: dict = Depends(require_admin)):
    """Remove duplicate transcript segments from databases created before
    transcript writes became idempotent. Declared before
    ``/{episode_id}/transcript`` so ``transcripts`` is not read as an id."""
    db = get_db()
    return {"deleted": db.dedupe_transcripts()}


@router.get("/{episode_id}/transcript", response_model=list[TranscriptSegment])
def get_transcript(episode_id: int, username: str = Depends(current_username)):
    db = get_db()
    user_episode(db, episode_id, username)

    segments = db.get_transcripts_for_episode(episode_id)
    if not segments:
        raise HTTPException(404, "No transcript found for this episode")
    from p3.speakers import display_names

    names = display_names(db.get_episode_speakers(episode_id))
    for segment in segments:
        segment["speaker_name"] = names.get(segment.get("speaker") or "")
    return segments


def _speakers(db, episode_id: int) -> list:
    from p3.speakers import display_names

    speakers = db.get_episode_speakers(episode_id)
    shown = display_names(speakers)
    return [
        {"label": label, **entry, "shown": shown[label]}
        for label, entry in speakers.items()
    ]


@router.get("/{episode_id}/speakers", response_model=SpeakerList)
def get_speakers(episode_id: int, username: str = Depends(current_username)):
    """Who speaks in an episode, most talk first."""
    db = get_db()
    user_episode(db, episode_id, username)
    return {"speakers": _speakers(db, episode_id)}


@router.put("/{episode_id}/speakers/{label}", response_model=SpeakerList)
def rename_speaker(
    episode_id: int,
    label: str,
    body: SpeakerRename,
    admin: dict = Depends(require_admin),
):
    """Give a speaker label a name, or forget it. Names are shared by
    everyone who has the episode, so only admins change them. Two labels
    may get the same name (diarization sometimes splits one voice in two).
    The digest keeps the names it was written with; Ask's passages are
    re-indexed by a queued job."""
    db = get_db()
    user_episode(db, episode_id, admin["username"])
    speakers = db.get_episode_speakers(episode_id)
    if label not in speakers:
        raise HTTPException(404, "No such speaker in this episode")
    speakers[label]["name"] = " ".join((body.name or "").split()) or None
    db.replace_episode_speakers(episode_id, speakers)
    job_id = None
    episode = db.get_episode_by_id(episode_id) or {}
    if episode.get("status") == "processed":
        from p3.api.tasks.ask import task_index_episode

        job_id = db.create_job(
            "index", episode_id=episode_id, username=admin["username"]
        )
        job_runner.enqueue(task_index_episode, job_id, episode_id)
    return {"speakers": _speakers(db, episode_id), "job_id": job_id}


@router.get("/{episode_id}/transcript/export")
def export_transcript(episode_id: int, username: str = Depends(current_username)):
    """Download this episode's transcript as a markdown file of sustained
    prose (no per-segment timestamps or line breaks)."""
    db = get_db()
    episode = user_episode(db, episode_id, username)

    segments = db.get_transcripts_for_episode(episode_id)
    if not segments:
        raise HTTPException(404, "No transcript found for this episode")

    from p3.downloader import _safe_filename

    content = DigestExporter(db).export_transcript_markdown(episode, segments)
    filename = f"{_safe_filename(episode['podcast_title'])}-{_safe_filename(episode['title'])}.md"
    return Response(
        content=content,
        media_type="text/markdown",
        headers=attachment(filename),
    )
