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
_WORD_RE = re.compile(r"\w+")
# Letters NFKD leaves whole, spelled out so "sœur" matches "soeur".
_LIGATURES = str.maketrans({"œ": "oe", "æ": "ae", "ø": "o", "ł": "l", "đ": "d"})
# Chinese and Japanese don't separate words: they are indexed by character
# pairs.
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]+")


def tokenize(text: str) -> List[str]:
    """Case-folded, accent-free words of ``text``, without stopwords, so "Été"
    matches "ete" and French and English share one index. Any script's
    words count; Chinese and Japanese runs become character pairs."""
    folded = unicodedata.normalize("NFKD", text.casefold().translate(_LIGATURES))
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    words: List[str] = []
    for word in _WORD_RE.findall(folded):
        word = word.strip("_")
        if not word:
            continue
        if _CJK_RE.search(word):
            for run in _CJK_RE.findall(word):
                words.extend(
                    [run]
                    if len(run) == 1
                    else [run[i : i + 2] for i in range(len(run) - 1)]
                )
            continue
        if word not in _STOPWORDS and (len(word) > 1 or word.isdigit()):
            words.append(word)
    return words


def term_counts(text: str) -> Counter:
    return Counter(tokenize(text))


def notes_text(description: Optional[str]) -> str:
    """Show notes as plain text: tags dropped, entities decoded, whitespace
    collapsed, cut to ``NOTES_MAX_CHARS``."""
    if not description:
        return ""
    # Feeds often escape their HTML once more: decode first, then strip.
    text = html.unescape(description)
    text = re.sub(r"<(br|p|li|div)[^>]*>", "\n", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    return text[:NOTES_MAX_CHARS]


# The model's own framing at the top of some summaries ("Here is a concise
# summary (approximately 300 words):").
_PREAMBLE_RE = re.compile(
    r"^\s*(here is|here's|voici|below is)\b[^\n]{0,120}?:\s*", re.IGNORECASE
)
# A quote is placed at the transcript passage holding this share of its words.
QUOTE_MATCH = 0.6


def clean_summary(text: str) -> str:
    return _PREAMBLE_RE.sub("", text, count=1).strip()


def locate_quote(quote: str, passages: List[Dict[str, Any]]) -> Optional[Dict]:
    """The transcript passage a digest quote comes from (most of its words
    in it), or None. Quotes are often lightly reworded by the digest."""
    words = set(tokenize(quote))
    if len(words) < 3:
        return None
    best, best_share = None, 0.0
    for p in passages:
        share = len(words & set(tokenize(p["text"]))) / len(words)
        if share > best_share:
            best, best_share = p, share
    return best if best_share >= QUOTE_MATCH else None


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
    transcript = list(chunks)
    if summary:
        text = clean_summary(summary.get("full_summary") or "")
        if text:
            chunks.append({"kind": KIND_SUMMARY, "text": text})
        topics = _as_list(summary.get("key_topics")) + _as_list(summary.get("themes"))
        if topics:
            chunks.append({"kind": KIND_TOPICS, "text": "; ".join(topics)})
        for quote in _as_list(summary.get("quotes")):
            # Placed in the transcript where it was said, when it can be found.
            source = locate_quote(quote, transcript)
            chunks.append(
                {
                    "kind": KIND_QUOTE,
                    "text": quote,
                    "start_s": source["start_s"] if source else None,
                    "end_s": source["end_s"] if source else None,
                }
            )
    for seq, chunk in enumerate(chunks):
        chunk.setdefault("start_s", None)
        chunk.setdefault("end_s", None)
        chunk["seq"] = seq
        chunk["prefix"] = prefix
        chunk["language"] = episode.get("language")
    return chunks
