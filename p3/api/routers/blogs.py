"""Blog post routes."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from p3.api.auth import current_username
from p3.api.content import find_post, list_posts
from p3.api.deps import get_db
from p3.api.job_queue import job_runner
from p3.api.models import BlogCreate, BlogOut
from p3.api.tasks.content import task_write_blog

router = APIRouter(prefix="/api/blogs", tags=["blogs"])

POSTS_DIR = "blog_posts"


def _blog(post: dict) -> dict:
    """A saved post with the grade its front matter records."""
    meta = post["meta"]
    return {
        **post,
        "final_grade": meta.get("final_grade"),
        "final_score": float(meta["final_score"]) if "final_score" in meta else None,
    }


@router.get("", response_model=list[BlogOut])
def list_blogs(username: str = Depends(current_username)):
    """List the user's blog posts (without full content)."""
    return [_blog(post) for post in list_posts(POSTS_DIR, username)]


@router.get("/{name}", response_model=BlogOut)
def get_blog(name: str, username: str = Depends(current_username)):
    """Get one of the user's blog posts, by file name without ``.md`` (or by
    slug, for the newest post with it)."""
    post = find_post(POSTS_DIR, username, name)
    if not post:
        raise HTTPException(404, f"Blog post '{name}' not found")
    return _blog(post)


@router.post("", response_model=dict)
def create_blog(body: BlogCreate, username: str = Depends(current_username)):
    """Generate a new blog post (English + Quebec French) from the day's
    summaries of the user's episodes, in their tone."""
    db = get_db()
    target_date = body.date or datetime.now().strftime("%Y-%m-%d")

    job_id = db.create_job(
        "write",
        username=username,
        args={
            "topic": body.topic,
            "date": target_date,
            "target_grade": body.target_grade,
            "provider": body.provider,
            "model": body.model,
        },
    )
    job_runner.enqueue(
        task_write_blog,
        job_id,
        body.topic,
        target_date,
        body.target_grade,
        body.provider,
        body.model,
        username,
    )
    return {"job_id": job_id}
