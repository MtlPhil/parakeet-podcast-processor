"""Generated post files (LinkedIn, blog) as the API serves them."""

from pathlib import Path
from typing import List, Optional, Tuple

from p3.api.deps import content_dir, get_db
from p3.writer import FRENCH_HEADING

_ENGLISH_HEADING = "## English"
_NOTES_MARKER = "\n---\n\n## Generation Notes"


def post_files(base: str, username: str) -> List[Path]:
    """A user's post files in ``base``, newest name first. The owner also
    gets the files from before users existed, kept at the top of ``base``."""
    folders = [content_dir(base, username)]
    if username == get_db().get_owner():
        folders.append(Path(base))
    paths = [p for folder in folders if folder.exists() for p in folder.glob("*.md")]
    return sorted(paths, key=lambda p: p.name, reverse=True)


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
