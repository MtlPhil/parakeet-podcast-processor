"""LinkedIn post routes."""

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException

from p3.api.deps import get_db
from p3.api.job_queue import job_runner
from p3.api.models import LinkedInCreate, LinkedInOut
from p3.api.tasks import task_write_linkedin

router = APIRouter(prefix="/api/linkedin", tags=["linkedin"])

LINKEDIN_DIR = Path("linkedin_posts")


def _parse_linkedin_file(path: Path) -> dict:
    """Parse a LinkedIn post markdown file to extract frontmatter and content."""
    text = path.read_text()
    meta = {}

    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].strip().splitlines():
                if ":" in line:
                    key, val = line.split(":", 1)
                    meta[key.strip()] = val.strip().strip('"')
            text = parts[2]

    stem = path.stem
    date_match = re.match(r"(\d{4}-\d{2}-\d{2})-(.*)", stem)
    if date_match:
        date_str = date_match.group(1)
        slug = date_match.group(2)
    else:
        date_str = ""
        slug = stem

    return {
        "slug": slug,
        "title": meta.get("title", slug.replace("-", " ").title()),
        "date": date_str,
        "filename": path.name,
        "episode_title": meta.get("source_episode"),
        "podcast_title": meta.get("source_podcast"),
        "content": text.strip(),
    }


@router.get("", response_model=list[LinkedInOut])
def list_linkedin_posts():
    """List all generated LinkedIn posts (without full content)."""
    if not LINKEDIN_DIR.exists():
        return []
    posts = []
    for path in sorted(LINKEDIN_DIR.glob("*.md"), reverse=True):
        info = _parse_linkedin_file(path)
        info["content"] = None  # omit full content in list view
        posts.append(info)
    return posts


@router.get("/{slug}", response_model=LinkedInOut)
def get_linkedin_post(slug: str):
    """Get a single LinkedIn post by slug."""
    if not LINKEDIN_DIR.exists():
        raise HTTPException(404, "No LinkedIn posts found")
    for path in LINKEDIN_DIR.glob("*.md"):
        info = _parse_linkedin_file(path)
        if info["slug"] == slug:
            return info
    raise HTTPException(404, f"LinkedIn post '{slug}' not found")


@router.post("", response_model=dict)
def create_linkedin_post(body: LinkedInCreate):
    """Generate a new LinkedIn post (English + Quebec French) from an episode."""
    db = get_db()
    episode = db.get_episode_by_id(body.episode_id)
    if not episode:
        raise HTTPException(404, "Episode not found")

    job_id = db.create_job(
        "write_linkedin", episode_id=body.episode_id, podcast_id=episode["podcast_id"]
    )
    job_runner.enqueue(task_write_linkedin, job_id, body.episode_id)
    return {"job_id": job_id}
