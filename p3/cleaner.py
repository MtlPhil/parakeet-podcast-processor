"""LLM-based transcript cleaning and summarization."""

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from .database import P3Database
from .llm import DEFAULT_OLLAMA_MODEL, DEFAULT_OLLAMA_URL, LLMClient

logger = logging.getLogger(__name__)

# Upper bound on transcript text sent in a single request. At roughly four
# characters per token this is ~25k tokens, which fits most context windows
# with room left for the prompt.
_MAX_TRANSCRIPT_CHARS = 100_000

_CLEAN_PROMPT = """Clean this podcast transcript by:
1. Removing filler words (um, uh, like, you know)
2. Fixing grammar and punctuation
3. Preserving technical terms and proper nouns exactly
4. Maintaining the speaker's voice and meaning
5. Breaking into clear paragraphs
6. Removing advertisements and sponsor reads — cut ad content entirely
   rather than summarizing it. This includes lines like "this episode is
   brought to you by...", promo codes, discount offers, and "visit
   [url]/[podcast name]" calls to action. Only remove clearly promotional
   material; leave the hosts' actual discussion untouched.

Return only the cleaned text, no additional commentary.

Transcript:
"""

# Summaries read the transcript as transcribed (fillers removed by regex,
# no LLM rewrite: rewriting cost two thirds of each episode's LLM time and
# let sponsor reads through anyway), so every prompt that reads it is told
# to ignore ads itself.
_AD_RULE = (
    "The transcript is raw and may include advertisements and sponsor reads: "
    'sponsor thank-yous, "this episode is brought to you by...", ad-free or '
    "Patreon offers, promo codes, discount offers and calls to visit a website. "
    "Ignore all of that completely: never mention sponsors, advertisers, their "
    "products or offers, and don't list a company that appears only in an ad. "
    "Base everything on the hosts' actual discussion."
)

_SUMMARY_PROMPT = """Analyze this podcast transcript and extract structured information in JSON format:

{
  "key_topics": ["topic1", "topic2", ...],
  "themes": ["theme1", "theme2", ...],
  "quotes": ["notable quote 1", "notable quote 2", ...],
  "startups": ["company1", "company2", ...]
}

Guidelines:
- key_topics: Main subjects discussed (3-5 topics)
- themes: Broader themes or patterns (2-4 themes)
- quotes: Memorable, insightful quotes (2-3 max)
- startups: Any companies, startups, or brands mentioned

""" + _AD_RULE + """

Transcript:
"""

# Short (~300 word) overview for a quick read. The long-form study notes
# (_SECTION_NOTES_PROMPT) complement it rather than replace it.
_SHORT_SUMMARY_PROMPT = """Write a summary of this podcast episode in about 300 words. This is not a marketing blurb or episode synopsis (the kind of thing you'd find on the podcast's website); skip scene-setting like "In this episode, X sits down with Y to discuss...". Go straight into substance: what the episode covers, the main arguments or claims made, and the concrete takeaways. Return only the summary text, no headings or commentary.

""" + _AD_RULE + """

Transcript:
"""

# Study notes for one section of a transcript (see _generate_long_summary).
# Small local models compress a whole-episode request into a paragraph or
# two no matter how the length is specified. Asking for notes on one section
# at a time keeps the output proportional to the source and gives even
# coverage of every part of the episode.
_SECTION_NOTES_PROMPT = """This is one part of a longer podcast transcript (not the whole episode). Write detailed study notes covering what happens in THIS PART — not a brief overview, and don't write as if summarizing a whole episode. For every distinct point or argument raised here, explain: what was claimed, the reasoning or evidence behind it, and any specific examples, numbers, or names mentioned. Write in full paragraphs, at least 150 words. Skip small talk and filler, but do not skip substantive content. Do not add framing like "This section discusses..." or "In this part..." — start directly with the content itself, as if continuing an ongoing set of notes.

""" + _AD_RULE + """

Transcript part:
"""

# Every digest also exists in Quebec French. Spelled out because models
# default to France French, which reads as foreign to a Quebec audience.
_QUEBEC_FRENCH = (
    "Quebec French (français québécois, as written in Quebec, not France "
    "French: Quebec vocabulary and usage, e.g. « courriel », « fin de "
    "semaine », « balado » for podcast)"
)

_TRANSLATE_PROMPT = f"""Translate this text into {_QUEBEC_FRENCH}. Keep names of people, companies and products, and all numbers, unchanged. Keep the formatting: paragraphs, headings and lists. Return only the translation, no commentary.

Text:
"""

# Plain lines rather than JSON: small local models often break JSON quoting
# when the text itself contains apostrophes, which French is full of.
_TRANSLATE_LIST_PROMPT = f"""Translate each line below, a short phrase, into {_QUEBEC_FRENCH}. Keep names unchanged. Return exactly one translated line per line, in the same order, with no numbering, bullets or commentary.

Lines:
"""

# Longer texts are translated a few paragraphs at a time, for the same
# reason the synopsis is written section by section: small local models
# shorten whatever doesn't fit comfortably in one reply.
_TRANSLATE_CHUNK_CHARS = 3000


def _split_paragraph_chunks(text: str, max_chars: int = _TRANSLATE_CHUNK_CHARS):
    """Group paragraphs into chunks of at most ``max_chars`` (a single longer
    paragraph is its own chunk)."""
    chunks: List[str] = []
    current: List[str] = []
    size = 0
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        if current and size + len(paragraph) > max_chars:
            chunks.append("\n\n".join(current))
            current, size = [], 0
        current.append(paragraph)
        size += len(paragraph)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _truncate_transcript(text: str, max_chars: int = _MAX_TRANSCRIPT_CHARS) -> str:
    """Truncate a transcript to fit within LLM context limits.

    Keeps the beginning and end of the transcript (most important for
    intros/conclusions) and inserts a marker where content was cut.
    """
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    logger.warning(
        "Transcript too long (%d chars), truncating to %d chars", len(text), max_chars
    )
    return (
        text[:half] + "\n\n[... transcript truncated for length ...]\n\n" + text[-half:]
    )


def _split_into_sections(text: str, num_sections: int) -> List[str]:
    """Split text into num_sections roughly-equal chunks, breaking on
    whitespace so words aren't cut in half."""
    text = text.strip()
    if num_sections <= 1 or not text:
        return [text] if text else []

    size = len(text) // num_sections
    sections = []
    start = 0
    for i in range(num_sections):
        if i == num_sections - 1:
            sections.append(text[start:])
            break
        end = min(start + size, len(text))
        while end < len(text) and not text[end].isspace():
            end += 1
        sections.append(text[start:end])
        start = end
    return [s.strip() for s in sections if s.strip()]


_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")

# Target chunk size for LLM-based transcript cleaning (see clean_transcript).
_CLEAN_CHUNK_CHARS = 6000

# Preamble/epilogue lines some models add around a cleaned chunk despite
# being told to return only the text (e.g. "Here's the cleaned transcript:").
_LLM_META_LINE_RE = re.compile(
    r"^(here('s| is) the cleaned( podcast)? transcript:?|note: .*rephrasing.*)$",
    re.IGNORECASE,
)


def _split_into_chunks(text: str, max_chars: int = _CLEAN_CHUNK_CHARS) -> List[str]:
    """Split text into chunks of at most ~max_chars, breaking only at
    sentence boundaries.

    A chunk that starts or ends mid-sentence invites the LLM to "repair"
    the fragment by dropping it or inventing a transition, so whole
    sentences are packed into each chunk instead. A single sentence longer
    than max_chars is kept whole rather than cut.
    """
    text = text.strip()
    if not text:
        return []

    sentences = _SENTENCE_BOUNDARY.split(text)

    chunks = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate) > max_chars and current:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


# Heuristic: a silence at least this long between consecutive segments is
# treated as a likely editing seam (an ad insertion, a music sting, a segment
# change) rather than a pause within continuous speech.
_TOPIC_GAP_SECONDS = 0.5

# Gap-delimited chunks shorter than this are merged into a neighbor so a
# one-line aside does not become its own LLM request.
_MIN_CHUNK_CHARS = 500


def _split_segments_by_gaps(
    segments: List[Dict[str, Any]],
    min_gap: float = _TOPIC_GAP_SECONDS,
    max_chars: int = _CLEAN_CHUNK_CHARS,
    min_chars: int = _MIN_CHUNK_CHARS,
    pack_chars: int = _CLEAN_CHUNK_CHARS,
) -> List[str]:
    """Split transcript segments into chunks at natural conversation
    breaks, using the gap between one segment's end and the next's start.

    Ads and segment transitions are spliced in at discrete edit points,
    which show up as pauses outside normal conversational rhythm. Splitting
    there tends to keep each chunk to roughly one topic, so an ad block
    usually reaches the LLM as a self-contained unit it can drop entirely.

    Any chunk still over max_chars (a long uninterrupted stretch with no
    qualifying gap) is further split on sentence boundaries; any chunk
    under min_chars is merged into a neighbor so a one-line aside doesn't
    become its own LLM call.

    Consecutive chunks are then packed together up to pack_chars (0 turns
    this off). Each LLM call has a fixed cost, and Whisper leaves a pause
    of min_gap or more after most sentences (Parakeet's segments run back
    to back), so without packing a Whisper transcript became ~110 calls of
    ~600 characters where a Parakeet one of the same length takes ~15.
    """
    if not segments:
        return []

    raw_chunks = []
    current: List[str] = []
    prev_end = segments[0]["timestamp_end"]
    for seg in segments:
        gap = seg["timestamp_start"] - prev_end
        if gap >= min_gap and current:
            raw_chunks.append(" ".join(current))
            current = []
        if seg["text"]:
            current.append(seg["text"])
        prev_end = seg["timestamp_end"]
    if current:
        raw_chunks.append(" ".join(current))

    merged: List[str] = []
    for chunk in raw_chunks:
        if merged and len(merged[-1]) < min_chars:
            merged[-1] = f"{merged[-1]} {chunk}"
        else:
            merged.append(chunk)
    if len(merged) > 1 and len(merged[0]) < min_chars:
        merged[1] = f"{merged[0]} {merged[1]}"
        merged = merged[1:]

    if pack_chars:
        packed: List[str] = []
        for chunk in merged:
            if packed and len(packed[-1]) + 1 + len(chunk) <= pack_chars:
                packed[-1] = f"{packed[-1]} {chunk}"
            else:
                packed.append(chunk)
        merged = packed

    final_chunks = []
    for chunk in merged:
        if len(chunk) > max_chars:
            final_chunks.extend(_split_into_chunks(chunk, max_chars))
        else:
            final_chunks.append(chunk)
    return final_chunks


def _truncate_chunks(
    chunks: List[str], max_chars: int = _MAX_TRANSCRIPT_CHARS
) -> List[str]:
    """Cap total chunk length by keeping chunks from the start and end and
    dropping the middle. Like _truncate_transcript, but cuts on chunk (topic)
    boundaries instead of an arbitrary character offset."""
    total = sum(len(c) for c in chunks)
    if total <= max_chars:
        return chunks

    half = max_chars // 2
    head, head_chars = [], 0
    for chunk in chunks:
        if head_chars and head_chars + len(chunk) > half:
            break
        head.append(chunk)
        head_chars += len(chunk)

    tail, tail_chars = [], 0
    for chunk in reversed(chunks):
        if tail_chars and tail_chars + len(chunk) > half:
            break
        tail.append(chunk)
        tail_chars += len(chunk)
    tail.reverse()

    logger.warning(
        "Transcript too long (%d chars across %d chunks), truncating to head+tail",
        total,
        len(chunks),
    )
    return head + ["[... transcript truncated for length ...]"] + tail


def _strip_llm_meta_lines(text: str) -> str:
    """Remove lines the LLM prepends/appends about its own output rather
    than the requested content (see _LLM_META_LINE_RE)."""
    lines = [ln for ln in text.split("\n") if not _LLM_META_LINE_RE.match(ln.strip())]
    return "\n".join(lines).strip()


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Robustly extract a JSON object from LLM output.

    Handles markdown code fences and finds the outermost balanced braces.
    """
    # Strip markdown code fences if present
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = text.replace("```", "")

    # Find the first '{' and then find its matching '}'
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    logger.warning("Found balanced braces but JSON decode failed")
                    return None

    return None


def _coerce_str_list(items: Any) -> List[str]:
    """Coerce a JSON value into a list of plain strings.

    The LLM is asked for lists of strings but sometimes returns objects,
    e.g. {"theme_name": "...", "description": "..."}. Each item is reduced
    to its most name-like string field so stored summaries always match the
    List[str] schema.
    """
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            for key in ("theme_name", "name", "title", "topic"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    result.append(value)
                    break
            else:
                str_value = next(
                    (v for v in item.values() if isinstance(v, str) and v), None
                )
                result.append(str_value if str_value else str(item))
        else:
            result.append(str(item))
    return result


class TranscriptCleaner:
    """Cleans transcripts and produces per-episode summaries with an LLM."""

    def __init__(
        self,
        db: P3Database,
        llm_provider: str = "ollama",
        llm_model: str = DEFAULT_OLLAMA_MODEL,
        api_key: Optional[str] = None,
        ollama_base_url: str = DEFAULT_OLLAMA_URL,
    ):
        self.db = db
        self.llm = LLMClient(
            provider=llm_provider,
            model=llm_model,
            api_key=api_key,
            ollama_base_url=ollama_base_url,
        )

    def _chat(self, system: str, user: str, max_tokens: int = 2000) -> str:
        """Send one request to the configured LLM provider."""
        return self.llm.chat(system, user, max_tokens=max_tokens)

    @staticmethod
    def _basic_clean_text(text: str) -> str:
        """Strip unambiguous filler words and collapse whitespace."""
        fillers = r"\b(um|uh|ah|er|hmm)\b"
        text = re.sub(fillers, "", text, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", text).strip()

    def clean_transcript(
        self, raw_text: str, segments: Optional[List[Dict[str, Any]]] = None
    ) -> str:
        """Remove filler words, ads and disfluencies from a transcript.

        Filler words are always stripped with a regex. When an LLM is
        configured, the text is then polished chunk by chunk: a single
        request over a whole transcript tends to come back heavily
        truncated. With timestamped segments, chunks follow natural
        conversation breaks (see _split_segments_by_gaps); otherwise they
        follow sentence boundaries. A chunk that fails keeps its
        regex-cleaned text so one bad response never loses the transcript.
        """
        text = self._basic_clean_text(raw_text)

        if self.llm.is_configured:
            try:
                if segments:
                    cleaned_segments = [
                        {**seg, "text": self._basic_clean_text(seg["text"])}
                        for seg in segments
                    ]
                    chunks = _truncate_chunks(_split_segments_by_gaps(cleaned_segments))
                else:
                    chunks = _split_into_chunks(_truncate_transcript(text))

                cleaned_chunks = []
                for i, chunk in enumerate(chunks, 1):
                    try:
                        cleaned = self._chat(
                            "You are an expert transcript editor.",
                            _CLEAN_PROMPT + chunk,
                            max_tokens=3000,
                        ).strip()
                        cleaned = _strip_llm_meta_lines(cleaned)
                        cleaned_chunks.append(cleaned if cleaned else chunk)
                    except Exception as e:
                        logger.warning(
                            "Chunk %d/%d cleaning failed, keeping original text for that chunk: %s",
                            i,
                            len(chunks),
                            e,
                        )
                        cleaned_chunks.append(chunk)
                if cleaned_chunks:
                    text = "\n\n".join(cleaned_chunks)
            except Exception as e:
                logger.warning("LLM cleaning failed, using basic cleaning: %s", e)

        return text

    def generate_summary(self, episode_id: int) -> Optional[Dict[str, Any]]:
        """Generate and store the digest for one episode.

        Produces the short summary plus structured topics, themes, quotes
        and companies. The long-form synopsis is generated separately on
        demand (see generate_synopsis) to keep the pipeline step fast.
        """
        segments = self.db.get_transcripts_for_episode(episode_id)
        full_text = "\n".join(segment["text"] for segment in segments)

        if not full_text.strip():
            return None

        # Fillers removed by regex only. An LLM rewrite of the whole transcript
        # (clean_transcript) took most of this step's time; the prompts skip
        # ads themselves (_AD_RULE).
        cleaned_text = self._basic_clean_text(full_text)

        # Generate structured summary using LLM
        summary_data = self._generate_structured_summary(cleaned_text)

        if summary_data and self.llm.is_configured:
            short_summary = self._generate_short_summary(cleaned_text)
            if short_summary:
                summary_data["summary"] = short_summary

        if summary_data:
            # add_summary replaces the whole row; carry over any synopsis
            # generated earlier so a re-digest does not discard it.
            existing = self.db.get_summary_by_episode(episode_id)
            existing_long_summary = existing.get("long_summary") if existing else None

            self.db.add_summary(
                episode_id=episode_id,
                key_topics=summary_data.get("key_topics", []),
                themes=summary_data.get("themes", []),
                quotes=summary_data.get("quotes", []),
                startups=summary_data.get("startups", []),
                full_summary=summary_data.get("summary", ""),
                long_summary=existing_long_summary,
                long_summary_fr=existing.get("long_summary_fr") if existing else None,
                digest_date=datetime.now(),
            )
            if self.llm.is_configured:
                self.translate_digest(episode_id)
            self.db.update_episode_status(episode_id, "processed")
            # The transcript and summary are all later steps need, so the
            # audio, by far the largest part of an episode, can go.
            self.db.remove_audio(episode_id)

        return summary_data

    def generate_synopsis(self, episode_id: int) -> Optional[str]:
        """Generate and store the long-form study-notes synopsis for one
        episode. Requested on demand; not part of the digest pipeline."""
        segments = self.db.get_transcripts_for_episode(episode_id)
        full_text = "\n".join(segment["text"] for segment in segments)

        if not full_text.strip():
            return None

        cleaned_text = self._basic_clean_text(full_text)
        synopsis = self._generate_long_summary(cleaned_text)
        if synopsis:
            self.db.update_summary_long_summary(episode_id, synopsis)
            self.db.update_summary_fields(
                episode_id, long_summary_fr=self.translate_text(synopsis)
            )
        return synopsis

    def translate_digest(self, episode_id: int, synopsis: bool = False) -> bool:
        """Store the Quebec French version of an episode's digest: summary,
        topics and themes, plus the synopsis if ``synopsis`` is set and there
        is one. Quotes stay verbatim. Returns False if there is no digest or
        the summary could not be translated."""
        summary = self.db.get_summary_by_episode(episode_id)
        if not summary:
            return False
        fields: Dict[str, Any] = {
            "full_summary_fr": self.translate_text(summary.get("full_summary") or ""),
            "key_topics_fr": self.translate_list(summary.get("key_topics") or []),
            "themes_fr": self.translate_list(summary.get("themes") or []),
        }
        if synopsis and summary.get("long_summary"):
            fields["long_summary_fr"] = self.translate_text(summary["long_summary"])
        self.db.update_summary_fields(
            episode_id, **{k: v for k, v in fields.items() if v is not None}
        )
        return fields["full_summary_fr"] is not None

    def translate_text(self, text: str) -> Optional[str]:
        """Quebec French translation of ``text``, or None on failure."""
        if not text.strip():
            return None
        try:
            parts = [
                self._chat(
                    "You are a professional English to Quebec French translator.",
                    _TRANSLATE_PROMPT + chunk,
                    max_tokens=2500,
                ).strip()
                for chunk in _split_paragraph_chunks(text)
            ]
        except Exception as e:
            logger.warning("Translation failed: %s", e)
            return None
        return "\n\n".join(parts) if all(parts) else None

    def translate_list(self, items: List[str]) -> Optional[List[str]]:
        """Quebec French translation of a list of short phrases, or None on
        failure (including a reply with a different number of items)."""
        if not items:
            return []
        try:
            response = self._chat(
                "You are a professional English to Quebec French translator.",
                _TRANSLATE_LIST_PROMPT + "\n".join(" ".join(i.split()) for i in items),
                max_tokens=800,
            )
        except Exception as e:
            logger.warning("List translation failed: %s", e)
            return None
        lines = [
            re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()
            for line in response.replace("```", "").splitlines()
        ]
        translated = [line for line in lines if line]
        if len(translated) != len(items):
            logger.warning(
                "List translation returned %d lines for %d items",
                len(translated),
                len(items),
            )
            if len(items) == 1:
                return None
            # Small models merge or drop lines; one item at a time can't.
            singles = [self.translate_list([item]) for item in items]
            if any(single is None for single in singles):
                return None
            return [single[0] for single in singles if single]
        return translated

    def _generate_short_summary(self, text: str) -> Optional[str]:
        """Generate the short (~300 word) summary as its own call — see
        _SHORT_SUMMARY_PROMPT."""
        truncated = _truncate_transcript(text)
        try:
            return self._chat(
                "You are an expert podcast analyst who writes concise, substantive summaries.",
                _SHORT_SUMMARY_PROMPT + truncated,
                max_tokens=600,
            ).strip()
        except Exception as e:
            logger.warning("Short summary generation failed: %s", e)
            return None

    def _generate_structured_summary(self, text: str) -> Optional[Dict[str, Any]]:
        """Generate structured summary using LLM."""
        if not self.llm.is_configured:
            return self._basic_extraction(text)

        truncated = _truncate_transcript(text)

        try:
            # Small models sometimes emit broken JSON; a second sample usually parses.
            result = None
            for _ in range(2):
                response = self._chat(
                    "You are an expert at analyzing podcast content. Return valid JSON only.",
                    _SUMMARY_PROMPT + truncated,
                    max_tokens=1000,
                )
                result = _extract_json(response)
                if result is not None:
                    break
            if result is None:
                logger.warning(
                    "Could not parse JSON from LLM response, falling back to basic extraction"
                )
                return self._basic_extraction(text)
            for field in ("key_topics", "themes", "quotes", "startups"):
                if field in result:
                    result[field] = _coerce_str_list(result[field])
            return result
        except Exception as e:
            logger.error("LLM summarization failed: %s", e)
            return self._basic_extraction(text)

    def _generate_long_summary(self, text: str) -> Optional[str]:
        """Generate long-form study notes, one transcript section at a time.

        Per-section requests keep the notes proportional to the episode: a
        longer episode gets more sections and therefore longer notes.
        """
        truncated = _truncate_transcript(text)
        num_sections = max(3, min(6, len(truncated) // 8000))
        sections = _split_into_sections(truncated, num_sections)

        notes = []
        for i, section in enumerate(sections, 1):
            try:
                note = self._chat(
                    "You are an expert podcast analyst writing detailed study notes.",
                    _SECTION_NOTES_PROMPT + section,
                    max_tokens=1000,
                ).strip()
                if note:
                    notes.append(note)
            except Exception as e:
                logger.warning("Section %d/%d notes failed: %s", i, len(sections), e)

        return "\n\n".join(notes) if notes else None

    def _basic_extraction(self, text: str) -> Dict[str, Any]:
        """Basic keyword extraction as fallback."""
        words = text.lower().split()
        word_freq: Dict[str, int] = {}
        for word in words:
            if len(word) > 4 and word.isalpha():
                word_freq[word] = word_freq.get(word, 0) + 1

        key_topics = [
            word
            for word, _ in sorted(word_freq.items(), key=lambda x: x[1], reverse=True)[
                :5
            ]
        ]

        # Simple company extraction (words ending in common suffixes)
        potential_companies = set()
        for word in text.split():
            if any(
                word.lower().endswith(suffix)
                for suffix in ["inc", "corp", "llc", "labs"]
            ):
                potential_companies.add(word)

        return {
            "key_topics": key_topics,
            "themes": ["general discussion"],
            "quotes": [],
            "startups": list(potential_companies),
            "summary": "Podcast episode discussion covering various topics.",
        }

    def process_all_transcribed(self) -> int:
        """Process all episodes with 'transcribed' status."""
        episodes = self.db.get_episodes_by_status("transcribed")
        processed_count = 0

        for episode in episodes:
            logger.info("Processing summary for: %s", episode["title"])
            if self.generate_summary(episode["id"]):
                processed_count += 1
                logger.info("Processed: %s", episode["title"])
            else:
                logger.warning("Failed to process: %s", episode["title"])

        return processed_count
