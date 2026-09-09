"""Blog post routes."""

import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException

from p3.api.deps import get_db
from p3.api.models import BlogCreate, BlogOut
from p3.api.tasks import task_write_blog

router = APIRouter(prefix="/api/blogs", tags=["blogs"])

BLOG_DIR = Path("blog_posts")


def _parse_blog_file(path: Path) -> dict:
    """Parse a blog post markdown file to extract frontmatter and content."""
    text = path.read_text()
    meta = {}

    # Parse YAML frontmatter
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].strip().splitlines():
                if ":" in line:
                    key, val = line.split(":", 1)
                    meta[key.strip()] = val.strip().strip('"')
            text = parts[2]

    # Derive slug from filename: YYYY-MM-DD-slug.md
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
        "final_grade": meta.get("final_grade"),
        "final_score": float(meta["final_score"]) if "final_score" in meta else None,
        "content": text.strip(),
    }


@router.get("", response_model=list[BlogOut])
def list_blogs():
    """List all generated blog posts (without full content)."""
    if not BLOG_DIR.exists():
        return []
    blogs = []
    for path in sorted(BLOG_DIR.glob("*.md"), reverse=True):
        info = _parse_blog_file(path)
        info["content"] = None  # omit full content in list view
        blogs.append(info)
    return blogs


@router.get("/{slug}", response_model=BlogOut)
def get_blog(slug: str):
    """Get a single blog post by slug."""
    if not BLOG_DIR.exists():
        raise HTTPException(404, "No blog posts found")
    for path in BLOG_DIR.glob("*.md"):
        info = _parse_blog_file(path)
        if info["slug"] == slug:
            return info
    raise HTTPException(404, f"Blog post '{slug}' not found")


@router.post("", response_model=dict)
def create_blog(body: BlogCreate, background_tasks: BackgroundTasks):
    """Generate a new blog post from podcast summaries."""
    db = get_db()
    target_date = body.date or datetime.now().strftime("%Y-%m-%d")

    job_id = db.create_job("write")
    background_tasks.add_task(
        task_write_blog, job_id, body.topic, target_date, body.target_grade
    )
    return {"job_id": job_id}
