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
    about because which
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
    r"^\s*(here is|here's|voici|below is)\b[^\n]{0,120}?"
    r"\b(summary|résumé|resume)\b[^\n]{0,60}?:\s*",
    re.IGNORECASE,
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


def _passage(
    group: List[Dict[str, Any]], names: Optional[Dict[str, str]]
) -> Dict[str, Any]:
    """One transcript passage. With speaker ``names`` ({label: name}), its
    text says "Name: " at the start and wherever the speaker changes, and
    ``speakers`` lists who speaks in it, most talk first."""
    chunk: Dict[str, Any] = {
        "kind": KIND_TRANSCRIPT,
        "start_s": group[0].get("timestamp_start"),
        "end_s": group[-1].get("timestamp_end"),
    }
    if not names:
        chunk["text"] = " ".join(s["text"].strip() for s in group)
        return chunk
    parts: List[str] = []
    talk: Dict[str, int] = {}
    previous = None
    for s in group:
        text = s["text"].strip()
        name = names.get(s.get("speaker") or "")
        if name:
            talk[name] = talk.get(name, 0) + len(text)
            if name != previous:
                text = f"{name}: {text}"
                if parts:
                    parts[-1] += "\n"
            previous = name
        parts.append(text)
    chunk["text"] = " ".join(parts).replace("\n ", "\n")
    chunk["speakers"] = ", ".join(sorted(talk, key=lambda n: -talk[n])) or None
    return chunk


def transcript_chunks(
    segments: List[Dict[str, Any]], names: Optional[Dict[str, str]] = None
) -> List[Dict[str, Any]]:
    """Group transcript segments (``timestamp_start``, ``timestamp_end``,
    ``text``, in order) into passages of about ``TARGET_SECONDS``, each
    starting with the last segment of the one before. ``names`` maps speaker
    labels to names (see ``_passage``)."""
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
        chunks.append(_passage(group, names))
        if j >= len(segments):
            break
        # Overlap: the next passage starts with this one's last segment,
        # unless this one is that single segment (no progress otherwise).
        i = j - 1 if j - 1 > i else j
    return chunks


# A cold open: clips from later in the episode played before it starts.
# A passage of the first ``COLD_OPEN_SECONDS`` is one when at least
# ``_REPEATED_SHARE`` of its runs of ``_SHINGLE`` words are said again later.
COLD_OPEN_SECONDS = 180.0
_SHINGLE = 6
_REPEATED_SHARE = 0.5


def _shingles(text: str) -> set:
    words = tokenize(text)
    return {tuple(words[i : i + _SHINGLE]) for i in range(len(words) - _SHINGLE + 1)}


def without_cold_open(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Transcript passages without the cold open's: those clips are indexed
    where they are said in full, and an early copy would take an answer's
    slots with a teaser cut short."""
    later: set = set()
    for c in chunks:
        if (c.get("start_s") or 0) >= COLD_OPEN_SECONDS:
            later |= _shingles(c["text"])
    kept = []
    for c in chunks:
        if (c.get("start_s") or 0) < COLD_OPEN_SECONDS:
            own = _shingles(c["text"])
            if own and len(own & later) >= _REPEATED_SHARE * len(own):
                continue
        kept.append(c)
    return kept


def build_chunks(
    episode: Dict[str, Any],
    segments: List[Dict[str, Any]],
    summary: Optional[Dict[str, Any]] = None,
    names: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Every passage of an episode, numbered in ``seq`` order: transcript
    passages first, then the show notes, summary, topics and quotes.
    ``episode`` needs ``title``, ``podcast_title``, ``date``, ``language``
    and ``description``; ``names`` maps speaker labels to names."""
    prefix = _prefix(
        episode.get("podcast_title") or "", episode["title"], episode.get("date")
    )
    chunks = without_cold_open(transcript_chunks(segments, names))
    # Taken before the show notes: quotes are placed in what was said.
    transcript = list(chunks)
    notes = notes_text(episode.get("description"))
    if notes:
        chunks.append({"kind": KIND_NOTES, "text": notes})
    if summary:
        # A French episode's summary in French, when the digest has one.
        french = episode.get("language") == "fr" and summary.get("full_summary_fr")
        text = clean_summary(
            (summary.get("full_summary_fr") if french else summary.get("full_summary"))
            or ""
        )
        if text:
            chunks.append({"kind": KIND_SUMMARY, "text": text})
        # Topics that are only common words come from the word-frequency
        # fallback ("about; because; think"): they would match anything.
        topics = [
            t
            for t in _as_list(summary.get("key_topics"))
            + _as_list(summary.get("themes"))
            if tokenize(t)
        ]
        # The word-frequency fallback (no LLM) marks its output with this one
        # theme; its "topics" are just frequent words.
        fallback = _as_list(summary.get("themes")) == ["general discussion"]
        if not fallback and (
            len(topics) >= 2 or (topics and len(tokenize(topics[0])) > 1)
        ):
            chunks.append({"kind": KIND_TOPICS, "text": "; ".join(topics)})
        for quote in _as_list(summary.get("quotes")):
            # Placed in the transcript where it was said, when it can be found.
            source = locate_quote(quote, transcript)
            chunks.append(
                {
                    "kind": KIND_QUOTE,
                    "text": quote,
                    "start_s": source.get("start_s") if source else None,
                    "end_s": source.get("end_s") if source else None,
                }
            )
    for seq, chunk in enumerate(chunks):
        chunk.setdefault("start_s", None)
        chunk.setdefault("end_s", None)
        chunk["seq"] = seq
        chunk["prefix"] = prefix
        chunk["language"] = episode.get("language")
    return chunks
