"""LinkedIn post routes."""

from fastapi import APIRouter, Depends, HTTPException

from p3.api.auth import current_username
from p3.api.content import find_post, list_posts
from p3.api.deps import get_db
from p3.api.job_queue import job_runner
from p3.api.models import LinkedInCreate, LinkedInOut
from p3.api.tasks import task_write_linkedin

router = APIRouter(prefix="/api/linkedin", tags=["linkedin"])

POSTS_DIR = "linkedin_posts"


def _linkedin(post: dict) -> dict:
    """A saved post with the episode its front matter records."""
    meta = post["meta"]
    return {
        **post,
        "episode_title": meta.get("source_episode"),
        "podcast_title": meta.get("source_podcast"),
    }


@router.get("", response_model=list[LinkedInOut])
def list_linkedin_posts(username: str = Depends(current_username)):
    """List the user's LinkedIn posts (without full content)."""
    return [_linkedin(post) for post in list_posts(POSTS_DIR, username)]


@router.get("/{name}", response_model=LinkedInOut)
def get_linkedin_post(name: str, username: str = Depends(current_username)):
    """Get one of the user's LinkedIn posts, by file name without ``.md``
    (or by slug, for the newest post with it)."""
    post = find_post(POSTS_DIR, username, name)
    if not post:
        raise HTTPException(404, f"LinkedIn post '{name}' not found")
    return _linkedin(post)


@router.post("", response_model=dict)
def create_linkedin_post(
    body: LinkedInCreate, username: str = Depends(current_username)
):
    """Generate a new LinkedIn post (English + Quebec French) from an episode
    in the user's library, in their tone."""
    db = get_db()
    episode = db.get_episode_by_id(body.episode_id)
    if not episode or not db.user_has_episode(username, body.episode_id):
        raise HTTPException(404, "Episode not found")

    job_id = db.create_job(
        "write_linkedin",
        episode_id=body.episode_id,
        podcast_id=episode["podcast_id"],
        username=username,
    )
    job_runner.enqueue(
        task_write_linkedin,
        job_id,
        body.episode_id,
        body.provider,
        body.model,
        username,
    )
    return {"job_id": job_id}
