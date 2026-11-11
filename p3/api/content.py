"""Generated post files (LinkedIn, blog) as the API serves them."""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from p3.api.deps import content_dir, get_db
from p3.writer import FRENCH_HEADING

_ENGLISH_HEADING = "## English"
_NOTES_MARKER = "\n---\n\n## Generation Notes"
_DATED_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})-(.*)")


def post_files(base: str, username: str) -> List[Path]:
    """A user's post files in ``base``, newest name first. The owner also
    gets the files from before users existed, kept at the top of ``base``."""
    folders = [content_dir(base, username)]
    if username == get_db().get_owner():
        folders.append(Path(base))
    paths = [p for folder in folders if folder.exists() for p in folder.glob("*.md")]
    return sorted(paths, key=lambda p: p.name, reverse=True)


def read_post(path: Path) -> Dict[str, Any]:
    """A saved post: ``meta`` (its front matter), ``date`` and ``slug`` (from
    its ``YYYY-MM-DD-slug.md`` name), ``title``, and its text as ``content``
    and ``content_fr``."""
    text = path.read_text(encoding="utf-8")
    meta = {}
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].strip().splitlines():
                key, colon, value = line.partition(":")
                if colon:
                    meta[key.strip()] = _unquoted(value.strip())
            text = parts[2]

    dated = _DATED_NAME.match(path.stem)
    date, slug = dated.groups() if dated else ("", path.stem)
    content, content_fr = split_languages(text)
    return {
        "slug": slug,
        "title": meta.get("title", slug.replace("-", " ").title()),
        "date": date,
        "filename": path.name,
        "meta": meta,
        "content": content,
        "content_fr": content_fr,
    }


def _unquoted(value: str) -> str:
    """A front-matter value without the one pair of quotes written around it
    (quotes that are part of the value stay)."""
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def list_posts(base: str, username: str) -> List[Dict[str, Any]]:
    """A user's posts in ``base``, newest first, without their text."""
    posts = [read_post(path) for path in post_files(base, username)]
    for post in posts:
        post["content"] = post["content_fr"] = None
    return posts


def find_post(base: str, username: str, name: str) -> Optional[Dict[str, Any]]:
    """One of a user's posts, by file name without ``.md``. A bare slug is
    accepted too and gives the newest post with it: two posts on one episode
    or topic, written on different days, share their slug."""
    paths = post_files(base, username)
    for path in paths:
        if path.stem == name:
            return read_post(path)
    for path in paths:
        dated = _DATED_NAME.match(path.stem)
        if dated and dated.group(2) == name:
            return read_post(path)
    return None


def split_languages(text: str) -> Tuple[str, Optional[str]]:
    """A post body as (English, Quebec French). French is None for posts
    without a French half. A blog post's generation notes go with both."""
    text = text.strip()
    if FRENCH_HEADING not in text:
        return text, None
    english, french = text.split(FRENCH_HEADING, 1)
    notes = ""
    if _NOTES_MARKER in french:
        french, notes = french.split(_NOTES_MARKER, 1)
        notes = _NOTES_MARKER + notes
    english = english.strip()
    if english.startswith(_ENGLISH_HEADING):
        english = english[len(_ENGLISH_HEADING) :].strip()
    return (english + notes).strip(), (french.strip() + notes).strip()
