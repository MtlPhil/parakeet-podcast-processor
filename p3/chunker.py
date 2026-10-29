"""Split an episode into passages for the Ask index, and tokenize text for its
keyword (BM25) search.

Transcript segments are a few seconds each; a passage groups consecutive
segments into about a minute of speech, overlapping its neighbour by one
segment so a sentence cut at a boundary is whole in one of them. The
episode's summary, topics and quotes become passages of their own. Each
passage carries a short ``prefix`` (podcast, episode, date) that is embedded
with it, so a passage that never names its subject still lands near it.
"""

import html
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional

TARGET_SECONDS = 60.0
# A passage also ends at this length, for segments without useful timing.
MAX_CHARS = 1500

KIND_TRANSCRIPT = "transcript"
KIND_SUMMARY = "summary"
KIND_TOPICS = "topics"
KIND_QUOTE = "quote"
KIND_NOTES = "notes"
# Show notes beyond this are mostly sponsor lists and links.
NOTES_MAX_CHARS = 3000

# Function words carry no meaning for keyword search, in either language.
_STOPWORDS = set("""
    a an and are as at be been but by can could did do does for from had has
    have he her his how i if in into is it its just like me my no not of on
    or our she so than that the their them then there these they this to
    too um uh us was we were what when where which who why will with would
    yeah you your yes oh okay ok also really very going get got know think
    au aux avec ce ces cet cette dans de des du elle elles en et est etait
    il ils je la le les leur leurs lui ma mais me mes moi mon ne nos notre
    nous on ou par pas pour qu que qui sa se ses son sur ta te tes toi ton
    tu un une vos votre vous ca ben euh bon alors donc puis comme fait faire
    """.split())
_WORD_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> List[str]:
    """Lowercased, accent-free words of ``text``, without stopwords, so "Été"
    matches "ete" and French and English share one index."""
    folded = unicodedata.normalize("NFKD", text.lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return [
        w
        for w in _WORD_RE.findall(folded)
        if w not in _STOPWORDS and (len(w) > 1 or w.isdigit())
    ]


def term_counts(text: str) -> Counter:
    return Counter(tokenize(text))


def notes_text(description: Optional[str]) -> str:
    """Show notes as plain text: tags dropped, entities decoded, whitespace
    collapsed, cut to ``NOTES_MAX_CHARS``."""
    if not description:
        return ""
    text = re.sub(r"<(br|p|li|div)[^>]*>", "\n", description, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    return text[:NOTES_MAX_CHARS]


def _prefix(podcast_title: str, episode_title: str, date: Optional[datetime]) -> str:
    day = f" ({date.strftime('%Y-%m-%d')})" if date else ""
    return f"{podcast_title}: {episode_title}{day}"


def _as_list(value: Any) -> List[str]:
    """A summaries JSON column (stored as text or already decoded) as strings."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [value] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def transcript_chunks(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group transcript segments (``timestamp_start``, ``timestamp_end``,
    ``text``, in order) into passages of about ``TARGET_SECONDS``, each
    starting with the last segment of the one before."""
    segments = [s for s in segments if (s.get("text") or "").strip()]
    chunks: List[Dict[str, Any]] = []
    i = 0
    while i < len(segments):
        group = [segments[i]]
        j = i + 1
        while j < len(segments):
            span = (segments[j].get("timestamp_end") or 0) - (
                group[0].get("timestamp_start") or 0
            )
            chars = sum(len(s["text"]) for s in group) + len(segments[j]["text"])
            if span > TARGET_SECONDS or chars > MAX_CHARS:
                break
            group.append(segments[j])
            j += 1
        chunks.append(
            {
                "kind": KIND_TRANSCRIPT,
                "start_s": group[0].get("timestamp_start"),
                "end_s": group[-1].get("timestamp_end"),
                "text": " ".join(s["text"].strip() for s in group),
            }
        )
        if j >= len(segments):
            break
        # Overlap: the next passage starts with this one's last segment,
        # unless this one is that single segment (no progress otherwise).
        i = j - 1 if j - 1 > i else j
    return chunks


def build_chunks(
    episode: Dict[str, Any],
    segments: List[Dict[str, Any]],
    summary: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Every passage of an episode, numbered in ``seq`` order: transcript
    passages first, then the show notes, summary, topics and quotes.
    ``episode`` needs ``title``, ``podcast_title``, ``date``, ``language``
    and ``description``."""
    prefix = _prefix(
        episode.get("podcast_title") or "", episode["title"], episode.get("date")
    )
    chunks = transcript_chunks(segments)
    notes = notes_text(episode.get("description"))
    if notes:
        chunks.append({"kind": KIND_NOTES, "text": notes})
    if summary:
        if (summary.get("full_summary") or "").strip():
            chunks.append({"kind": KIND_SUMMARY, "text": summary["full_summary"]})
        topics = _as_list(summary.get("key_topics")) + _as_list(summary.get("themes"))
        if topics:
            chunks.append({"kind": KIND_TOPICS, "text": "; ".join(topics)})
        for quote in _as_list(summary.get("quotes")):
            chunks.append({"kind": KIND_QUOTE, "text": quote})
    for seq, chunk in enumerate(chunks):
        chunk.setdefault("start_s", None)
        chunk.setdefault("end_s", None)
        chunk["seq"] = seq
        chunk["prefix"] = prefix
        chunk["language"] = episode.get("language")
    return chunks
