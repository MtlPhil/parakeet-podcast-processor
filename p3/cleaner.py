"""LLM-based summaries of transcripts: digest, synopsis and translation.

A digest is stored complete or not at all: a step that fails raises
DigestError and leaves the episode as it was, so it can be retried.
"""

import html
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

# Speech recognition misspells names, and a transcript never says how a
# name is written. The show's own title and notes do, and usually say who
# the hosts and guests are, so every prompt that reads the transcript gets
# them first (_episode_context).
_NAMES_RULE = (
    "The episode title and show notes below are published by the show. "
    "Speech recognition often misspells names in the transcript, so use them "
    "to tell who the hosts and guests are and to spell the names of people, "
    "companies and products exactly as they are written there. Take nothing "
    "else from them: everything you write must come from the transcript, and "
    "ignore any links, sponsors or promotions in the notes."
)
# Show notes past this are mostly links, credits and sponsor lists.
_NOTES_MAX_CHARS = 3000


def _notes_text(description: Optional[str]) -> str:
    """Show notes as plain text: tags dropped, entities decoded, whitespace
    collapsed, cut to ``_NOTES_MAX_CHARS``."""
    if not isinstance(description, str) or not description:
        return ""
    # Feeds often escape their HTML once more: decode first, then strip.
    text = html.unescape(description)
    text = re.sub(r"<(br|p|li|div)[^>]*>", "\n", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    return text[:_NOTES_MAX_CHARS]


def _episode_context(episode: Optional[Dict[str, Any]]) -> str:
    """The show's title and notes for an episode, with _NAMES_RULE, to put
    before a prompt that reads its transcript ("" if there are none)."""
    if not episode:
        return ""
    title = episode.get("title")
    title = title.strip() if isinstance(title, str) else ""
    notes = _notes_text(episode.get("description"))
    if not title and not notes:
        return ""
    lines = [_NAMES_RULE, ""]
    podcast = episode.get("podcast_title")
    if isinstance(podcast, str) and podcast.strip():
        lines.append(f"Podcast: {podcast.strip()}")
    if title:
        lines.append(f"Episode title: {title}")
    if notes:
        # The second unescape can bring back tags: the notes mustn't close
        # their own fence.
        notes = re.sub(r"</?\s*show_notes[^>]*>", " ", notes, flags=re.I)
        lines += ["<show_notes>", notes, "</show_notes>"]
    return "\n".join(lines) + "\n\n"


# _NAMES_RULE alone isn't enough for a small model: shown "Revolute" ten
# times in the transcript and "Revolut" once in the notes, it wrote both,
# and then "Revolutionary". So the transcript the prompts read already uses
# the notes' spelling (_respell_names).
_NAME_MIN_LEN = 5
_NAME_MIN_SIMILARITY = 0.8
_NAME_MAX_LEN_DIFF = 2
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def _is_other_form(a: str, b: str) -> bool:
    """Whether two lowercase words are forms of one word ("europe" and
    "europeans", "texas" and "texans", "canadian" and "canadiens": a
    singular is never respelled as a plural) rather than one misspelling
    the other. A trailing "e" alone is a misspelling ("revolute")."""
    if a.endswith("s") != b.endswith("s"):
        return True
    for x, y in ((a, b), (a.removesuffix("s"), b.removesuffix("s"))):
        short, long = sorted((x, y), key=len)
        if long != short and long.startswith(short) and long[len(short) :] != "e":
            return True
    return False


def _respell_names(text: str, source: str) -> str:
    """``text`` with names misspelled by speech recognition spelled as in
    ``source`` (the episode's title and show notes).

    A name is a word of ``source`` with a capital letter, not all capitals,
    at least _NAME_MIN_LEN letters, that the transcript never uses as an
    ordinary lowercase word. A transcript word is taken for a misspelling of
    it when:
    - it is capitalized in most of its uses inside a sentence (speech
      recognition capitalizes names; a sentence's first word says nothing);
    - it is used more often than the name's own spelling (a recognizer that
      writes "Tuesday" nine times means "Thursday" when it writes it);
    - it starts with the same letter, is at least _NAME_MIN_SIMILARITY
      alike, at most _NAME_MAX_LEN_DIFF letters longer or shorter, and is
      not another form of it (_is_other_form) or a word of ``source``.
    Every use of it is then replaced.

    Known risk: two different names a letter apart are merged when only one
    is in the notes (Marinaro in the notes turns a Mitch Mariner, meaning
    Marner, into Marinaro)."""
    from difflib import SequenceMatcher

    if not text or not source:
        return text
    count: Dict[str, int] = {}
    capitalized: Dict[str, int] = {}
    inside: Dict[str, int] = {}
    for m in _WORD.finditer(text):
        word, lower = m.group(), m.group().lower()
        count[lower] = count.get(lower, 0) + 1
        before = text[: m.start()].rstrip()[-1:]
        if before and before not in ".!?":
            inside[lower] = inside.get(lower, 0) + 1
            if word[0].isupper():
                capitalized[lower] = capitalized.get(lower, 0) + 1
    lowercase_words = {m.group() for m in _WORD.finditer(text) if m.group().islower()}

    source_words = set(_WORD.findall(source))
    source_lower = {w.lower() for w in source_words}
    names = {
        w.lower(): w
        for w in source_words
        if len(w) >= _NAME_MIN_LEN
        and not w.isupper()
        and any(c.isupper() for c in w)
        and w.lower() not in lowercase_words
    }

    replacements: Dict[str, str] = {}
    for lower, n in count.items():
        if len(lower) < _NAME_MIN_LEN or lower in source_lower:
            continue
        uses_inside = inside.get(lower, 0)
        if not uses_inside or capitalized.get(lower, 0) * 2 <= uses_inside:
            continue
        best, best_ratio = None, _NAME_MIN_SIMILARITY
        for name_lower, name in names.items():
            if (
                name_lower[0] != lower[0]
                or abs(len(name_lower) - len(lower)) > _NAME_MAX_LEN_DIFF
                or count.get(name_lower, 0) >= n
                or _is_other_form(name_lower, lower)
            ):
                continue
            ratio = SequenceMatcher(None, lower, name_lower).ratio()
            if ratio >= best_ratio:
                best, best_ratio = name, ratio
        if best:
            replacements[lower] = best
    if not replacements:
        return text
    logger.info("Respelled from the show notes: %s", replacements)
    return _WORD.sub(lambda m: replacements.get(m.group().lower(), m.group()), text)


_SUMMARY_PROMPT = (
    """Analyze this podcast transcript and extract structured information in JSON format:

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

"""
    + _AD_RULE
    + """

Transcript:
"""
)

# Short (~300 word) overview for a quick read. The long-form study notes
# (_SECTION_NOTES_PROMPT) complement it rather than replace it.
_SHORT_SUMMARY_PROMPT = (
    """Write a summary of this podcast episode in about 300 words. This is not a marketing blurb or episode synopsis (the kind of thing you'd find on the podcast's website); skip scene-setting like "In this episode, X sits down with Y to discuss...". Go straight into substance: what the episode covers, the main arguments or claims made, and the concrete takeaways. Return only the summary text, no headings or commentary.

"""
    + _AD_RULE
    + """

Transcript:
"""
)

# Study notes for one section of a transcript (see _generate_long_summary).
# Small local models compress a whole-episode request into a paragraph or
# two no matter how the length is specified. Asking for notes on one section
# at a time keeps the output proportional to the source and gives even
# coverage of every part of the episode.
_SECTION_NOTES_PROMPT = (
    """This is one part of a longer podcast transcript (not the whole episode). Write detailed study notes covering what happens in THIS PART — not a brief overview, and don't write as if summarizing a whole episode. For every distinct point or argument raised here, explain: what was claimed, the reasoning or evidence behind it, and any specific examples, numbers, or names mentioned. Write in full paragraphs, at least 150 words. Skip small talk and filler, but do not skip substantive content. Do not add framing like "This section discusses..." or "In this part..." — start directly with the content itself, as if continuing an ongoing set of notes.

"""
    + _AD_RULE
    + """

Transcript part:
"""
)

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


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """The JSON object in LLM output, or None. Text around it (a preamble,
    code fences, a closing remark) is ignored."""
    start = text.find("{")
    if start == -1:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text, start)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


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


class DigestError(RuntimeError):
    """A digest or synopsis could not be produced; the message says why."""


class TranscriptCleaner:
    """Produces per-episode digests, synopses and translations with an LLM."""

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

    def _transcript(self, episode_id: int) -> str:
        """An episode's transcript as the prompts read it: fillers removed by
        regex, names spelled as the show writes them (_respell_names),
        nothing else rewritten (the prompts skip ads themselves, see
        _AD_RULE). Empty if the episode has no transcript."""
        segments = self.db.get_transcripts_for_episode(episode_id)
        text = self._basic_clean_text(" ".join(s["text"] for s in segments))
        episode = self.db.get_episode_by_id(episode_id)
        if not isinstance(episode, dict):
            return text
        title = episode.get("title")
        title = title if isinstance(title, str) else ""
        notes = _notes_text(episode.get("description"))
        return _respell_names(text, f"{title}\n{notes}")

    def _require_llm(self):
        if not self.llm.is_configured:
            raise DigestError(f"No API key is set for {self.llm.provider}")

    def generate_summary(self, episode_id: int) -> None:
        """Generate and store the digest for one episode: the short summary
        plus structured topics, themes, quotes and companies, then their
        Quebec French versions. The long-form synopsis is generated
        separately on demand (see generate_synopsis) to keep this step fast.

        Raises DigestError, storing nothing, if there is no transcript, no
        LLM, or an LLM step fails. Only the translation may fail without
        failing the digest: it can be redone on its own (translate_digest).
        """
        text = self._transcript(episode_id)
        if not text:
            raise DigestError("The episode has no transcript")
        self._require_llm()

        context = _episode_context(self.db.get_episode_by_id(episode_id))
        data = self._generate_structured_summary(text, context)
        summary = self._generate_short_summary(text, context)

        # add_summary replaces the whole row; carry over any synopsis
        # generated earlier so a re-digest does not discard it.
        existing = self.db.get_summary_by_episode(episode_id) or {}
        with self.db.transaction():
            self.db.add_summary(
                episode_id=episode_id,
                key_topics=data.get("key_topics", []),
                themes=data.get("themes", []),
                quotes=data.get("quotes", []),
                startups=data.get("startups", []),
                full_summary=summary,
                long_summary=existing.get("long_summary"),
                long_summary_fr=existing.get("long_summary_fr"),
                digest_date=datetime.now(),
            )
            self.db.update_episode_status(episode_id, "processed")
        self.translate_digest(episode_id)
        # The transcript and summary are all later steps need, so the
        # audio, by far the largest part of an episode, can go.
        self.db.remove_audio(episode_id)

    def refresh_digest(self, episode_id: int) -> bool:
        """Redo an episode's digest, with the show's title and notes
        (_episode_context), keeping its date, and its synopsis too if it has
        one. Returns whether there was a synopsis.

        All or nothing, French included: a step that fails raises
        DigestError and leaves the stored digest as it was."""
        text = self._transcript(episode_id)
        existing = self.db.get_summary_by_episode(episode_id)
        if not text or not existing:
            raise DigestError("No transcript or no digest to refresh")
        self._require_llm()

        context = _episode_context(self.db.get_episode_by_id(episode_id))
        data = self._generate_structured_summary(text, context)
        summary = self._generate_short_summary(text, context)
        french: Dict[str, Any] = {
            "full_summary_fr": self.translate_text(summary),
            "key_topics_fr": self.translate_list(data.get("key_topics") or []),
            "themes_fr": self.translate_list(data.get("themes") or []),
        }
        synopsis = existing.get("long_summary")
        if synopsis:
            synopsis = self._generate_long_summary(text, context)
            french["long_summary_fr"] = self.translate_text(synopsis)
        failed = [field for field, value in french.items() if value is None]
        if failed:
            raise DigestError(f"Translating {', '.join(failed)} failed")

        with self.db.transaction():
            self.db.add_summary(
                episode_id=episode_id,
                key_topics=data.get("key_topics", []),
                themes=data.get("themes", []),
                quotes=data.get("quotes", []),
                startups=data.get("startups", []),
                full_summary=summary,
                long_summary=synopsis,
                digest_date=existing.get("digest_date"),
            )
            self.db.update_summary_fields(episode_id, **french)
        return bool(synopsis)

    def generate_synopsis(self, episode_id: int) -> str:
        """Generate and store the long-form study-notes synopsis for one
        episode, in English and Quebec French. Requested on demand; not part
        of the digest pipeline. Returns the English synopsis.

        All or nothing: without a transcript or a digest, or if a section or
        the translation fails, nothing is stored and DigestError says why."""
        text = self._transcript(episode_id)
        if not text:
            raise DigestError("The episode has no transcript")
        # The synopsis is stored on the digest's row.
        if not self.db.get_summary_by_episode(episode_id):
            raise DigestError("The episode has no digest yet")

        context = _episode_context(self.db.get_episode_by_id(episode_id))
        synopsis = self._generate_long_summary(text, context)
        synopsis_fr = self.translate_text(synopsis)
        if not synopsis_fr:
            raise DigestError("Translating the synopsis to French failed")
        with self.db.transaction():
            self.db.update_summary_long_summary(episode_id, synopsis)
            self.db.update_summary_fields(episode_id, long_summary_fr=synopsis_fr)
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

    def _generate_short_summary(self, text: str, context: str = "") -> str:
        """The short (~300 word) summary, as its own request (see
        _SHORT_SUMMARY_PROMPT). ``context`` is _episode_context's."""
        try:
            summary = self._chat(
                "You are an expert podcast analyst who writes concise, substantive summaries.",
                context + _SHORT_SUMMARY_PROMPT + _truncate_transcript(text),
                max_tokens=600,
            ).strip()
        except Exception as e:
            raise DigestError(f"The summary failed: {e}") from e
        if not summary:
            raise DigestError("The summary came back empty")
        return summary

    def _generate_structured_summary(
        self, text: str, context: str = ""
    ) -> Dict[str, Any]:
        """Topics, themes, quotes and companies, as lists of strings.
        ``context`` is _episode_context's."""
        prompt = context + _SUMMARY_PROMPT + _truncate_transcript(text)
        result = None
        # Small models sometimes emit broken JSON; a second sample usually parses.
        for _ in range(2):
            try:
                response = self._chat(
                    "You are an expert at analyzing podcast content. Return valid JSON only.",
                    prompt,
                    max_tokens=1000,
                )
            except Exception as e:
                raise DigestError(f"Topics and themes failed: {e}") from e
            result = _extract_json(response)
            if result is not None:
                break
        if result is None:
            raise DigestError("The topics and themes reply wasn't JSON")
        for field in ("key_topics", "themes", "quotes", "startups"):
            if field in result:
                result[field] = _coerce_str_list(result[field])
        return result

    def _generate_long_summary(self, text: str, context: str = "") -> str:
        """Generate long-form study notes, one transcript section at a time.

        Per-section requests keep the notes proportional to the episode: a
        longer episode gets more sections and therefore longer notes. Raises
        DigestError if any section fails, rather than return notes with a
        part of the episode missing. ``context`` (_episode_context's) goes
        with every section.
        """
        truncated = _truncate_transcript(text)
        num_sections = max(3, min(6, len(truncated) // 8000))
        sections = _split_into_sections(truncated, num_sections)

        notes = []
        for i, section in enumerate(sections, 1):
            try:
                note = self._chat(
                    "You are an expert podcast analyst writing detailed study notes.",
                    context + _SECTION_NOTES_PROMPT + section,
                    max_tokens=1000,
                ).strip()
            except Exception as e:
                raise DigestError(
                    f"Section {i}/{len(sections)} notes failed: {e}"
                ) from e
            if not note:
                raise DigestError(f"Section {i}/{len(sections)} notes came back empty")
            notes.append(note)

        return "\n\n".join(notes)
