"""LLM-based transcript cleaning and summarization."""

import json
import logging
import re
from datetime import datetime, date
from typing import Dict, List, Optional, Any
import httpx

from .database import P3Database

logger = logging.getLogger(__name__)

# Optional Ollama support
try:
    import ollama
    OLLAMA_AVAILABLE = True
except ImportError:
    OLLAMA_AVAILABLE = False

# Approximate token-to-character ratio for truncation.
# Most LLMs average ~4 chars per token; we leave headroom for the prompt.
_MAX_TRANSCRIPT_CHARS = 100_000  # ~25k tokens, safe for most model context windows

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

Transcript:
"""

# The short summary — a real overview in ~300 words, not a synopsis. Kept
# alongside the much longer section-by-section notes (_SECTION_NOTES_PROMPT)
# rather than replaced by them, since the two serve different purposes: this
# one for a quick read, the notes for depth.
_SHORT_SUMMARY_PROMPT = """Write a summary of this podcast episode in about 300 words. This is not a marketing blurb or episode synopsis (the kind of thing you'd find on the podcast's website); skip scene-setting like "In this episode, X sits down with Y to discuss...". Go straight into substance: what the episode covers, the main arguments or claims made, and the concrete takeaways. Return only the summary text, no headings or commentary.

Transcript:
"""

# Study notes for one section of a transcript (see _generate_long_summary).
# A single call asking for a 500-800 word summary of the WHOLE episode got
# ignored by this small local model — it wrote 2-3 sentences regardless of
# the instruction, then ~300 words with much stronger prompting, still well
# short. A model that can compress the whole episode down to one paragraph
# will keep doing that no matter how it's worded. Asking for notes on one
# section at a time removes that option — it can only write about what's in
# front of it — which is also a more genuinely "Coles Notes" shape: real
# coverage of every part of the episode, not one narrative overview.
_SECTION_NOTES_PROMPT = """This is one part of a longer podcast transcript (not the whole episode). Write detailed study notes covering what happens in THIS PART — not a brief overview, and don't write as if summarizing a whole episode. For every distinct point or argument raised here, explain: what was claimed, the reasoning or evidence behind it, and any specific examples, numbers, or names mentioned. Write in full paragraphs, at least 150 words. Skip small talk and filler, but do not skip substantive content. Do not add framing like "This section discusses..." or "In this part..." — start directly with the content itself, as if continuing an ongoing set of notes.

Transcript part:
"""


def _truncate_transcript(text: str, max_chars: int = _MAX_TRANSCRIPT_CHARS) -> str:
    """Truncate a transcript to fit within LLM context limits.

    Keeps the beginning and end of the transcript (most important for
    intros/conclusions) and inserts a marker where content was cut.
    """
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    logger.warning("Transcript too long (%d chars), truncating to %d chars", len(text), max_chars)
    return text[:half] + "\n\n[... transcript truncated for length ...]\n\n" + text[-half:]


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
    """Robustly extract a JSON object from LLM output.

    Handles markdown code fences and finds the outermost balanced braces.
    """
    # Strip markdown code fences if present
    text = re.sub(r'```(?:json)?\s*', '', text)
    text = text.replace('```', '')

    # Find the first '{' and then find its matching '}'
    start = text.find('{')
    if start == -1:
        return None

    depth = 0
    for i in range(start, len(text)):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    logger.warning("Found balanced braces but JSON decode failed")
                    return None

    return None


def _coerce_str_list(items: Any) -> List[str]:
    """Coerce a JSON value into a list of plain strings.

    The summary LLM is asked for lists of strings (key_topics, themes,
    quotes, startups) but sometimes returns objects instead, e.g.
    {"theme_name": "...", "description": "..."} in place of a theme string.
    That shape mismatch was reaching the database uncaught and crashing the
    summary read endpoint later (Pydantic's List[str] rejects the dicts).
    Coerce each item to a string here so a malformed shape degrades
    gracefully instead of corrupting stored data.
    """
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            for key in ('theme_name', 'name', 'title', 'topic'):
                value = item.get(key)
                if isinstance(value, str) and value:
                    result.append(value)
                    break
            else:
                str_value = next((v for v in item.values() if isinstance(v, str) and v), None)
                result.append(str_value if str_value else str(item))
        else:
            result.append(str(item))
    return result


class TranscriptCleaner:
    def __init__(self, db: P3Database, llm_provider: str = "openai",
                 llm_model: str = "gpt-3.5-turbo", api_key: str = None,
                 ollama_base_url: str = "http://localhost:11434"):
        self.db = db
        self.llm_provider = llm_provider.lower()
        self.llm_model = llm_model
        self.api_key = api_key
        self.ollama_base_url = ollama_base_url

        # Load API key from environment if not provided
        if not self.api_key and self.llm_provider not in ("ollama",):
            import os
            if self.llm_provider == "openai":
                self.api_key = os.getenv("OPENAI_API_KEY")
            elif self.llm_provider == "anthropic":
                self.api_key = os.getenv("ANTHROPIC_API_KEY")

    def _chat_openai(self, system: str, user: str, max_tokens: int = 2000) -> str:
        """Send a chat completion request to OpenAI."""
        with httpx.Client(timeout=120.0) as client:
            response = client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": self.llm_model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user}
                    ],
                    "temperature": 0.2,
                    "max_tokens": max_tokens
                }
            )
        if response.status_code != 200:
            raise RuntimeError(f"OpenAI API error: {response.status_code} - {response.text}")
        return response.json()["choices"][0]["message"]["content"].strip()

    def _chat_ollama(self, system: str, user: str, max_tokens: int = 2000) -> str:
        """Send a chat completion request to Ollama."""
        if not OLLAMA_AVAILABLE:
            raise RuntimeError("Ollama Python package is not installed")
        response = ollama.chat(
            model=self.llm_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
            # Unload the model right after this response instead of leaving
            # it (multiple GB) resident in Ollama's own process — that's
            # twice now caused an out-of-memory crash sitting idle after a
            # digest batch finished.
            keep_alive=0,
            options={
                # Hard cap on generated tokens: without this a small model
                # that slips into a repetition loop just keeps generating
                # until it exhausts the context window (observed: 74k+
                # tokens and 51 minutes for what should be a short response,
                # pinning the CPU and ~4GB of RAM the whole time). OpenAI's
                # path already had this via max_tokens; Ollama's didn't.
                "num_predict": max_tokens,
                # Slightly stronger than Ollama's 1.1 default, to make that
                # kind of repetition loop less likely to start in the first
                # place.
                "repeat_penalty": 1.3,
            },
        )
        return response['message']['content'].strip()

    def _chat(self, system: str, user: str, max_tokens: int = 2000) -> str:
        """Route a chat request to the configured LLM provider."""
        if self.llm_provider == "openai":
            return self._chat_openai(system, user, max_tokens=max_tokens)
        elif self.llm_provider == "ollama":
            return self._chat_ollama(system, user, max_tokens=max_tokens)
        elif self.llm_provider == "anthropic":
            raise NotImplementedError(
                "Anthropic backend is not yet implemented. "
                "Use 'ollama' or 'openai' as llm_provider."
            )
        else:
            raise ValueError(f"Unsupported LLM provider: {self.llm_provider}")

    def clean_transcript(self, raw_text: str) -> str:
        """Clean transcript by removing filler words and improving readability."""
        text = raw_text

        # Remove unambiguous verbal filler words only
        fillers = r'\b(um|uh|ah|er|hmm)\b'
        text = re.sub(fillers, '', text, flags=re.IGNORECASE)

        # Clean up extra whitespace
        text = re.sub(r'\s+', ' ', text).strip()

        # Use LLM for advanced cleaning if available
        if self.api_key or self.llm_provider == "ollama":
            try:
                truncated = _truncate_transcript(text)
                text = self._chat(
                    "You are an expert transcript editor.",
                    _CLEAN_PROMPT + truncated,
                    max_tokens=16_000,
                )
            except Exception as e:
                logger.warning("LLM cleaning failed, using basic cleaning: %s", e)

        return text

    def generate_summary(self, episode_id: int) -> Optional[Dict[str, Any]]:
        """Generate structured summary of an episode."""
        segments = self.db.get_transcripts_for_episode(episode_id)
        full_text = "\n".join(segment['text'] for segment in segments)

        if not full_text.strip():
            return None

        # Clean the transcript first
        cleaned_text = self.clean_transcript(full_text)

        # Generate structured summary using LLM
        summary_data = self._generate_structured_summary(cleaned_text)

        # The short (~300 word) and long (section-by-section "Coles Notes")
        # summaries are each their own dedicated call — only attempt them
        # when an LLM is actually configured, same condition
        # _generate_structured_summary uses before falling back to
        # _basic_extraction.
        long_summary = None
        if summary_data and (self.api_key or self.llm_provider == "ollama"):
            short_summary = self._generate_short_summary(cleaned_text)
            if short_summary:
                summary_data['summary'] = short_summary
            long_summary = self._generate_long_summary(cleaned_text)

        if summary_data:
            self.db.add_summary(
                episode_id=episode_id,
                key_topics=summary_data.get('key_topics', []),
                themes=summary_data.get('themes', []),
                quotes=summary_data.get('quotes', []),
                startups=summary_data.get('startups', []),
                full_summary=summary_data.get('summary', ''),
                long_summary=long_summary,
                digest_date=datetime.now()
            )
            self.db.update_episode_status(episode_id, 'processed')

        return summary_data

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
        if not self.api_key and self.llm_provider != "ollama":
            return self._basic_extraction(text)

        truncated = _truncate_transcript(text)

        try:
            response = self._chat(
                "You are an expert at analyzing podcast content. Return valid JSON only.",
                _SUMMARY_PROMPT + truncated,
                max_tokens=1000,
            )
            result = _extract_json(response)
            if result is None:
                logger.warning("Could not parse JSON from LLM response, falling back to basic extraction")
                return self._basic_extraction(text)
            for field in ('key_topics', 'themes', 'quotes', 'startups'):
                if field in result:
                    result[field] = _coerce_str_list(result[field])
            return result
        except Exception as e:
            logger.error("LLM summarization failed: %s", e)
            return self._basic_extraction(text)

    def _generate_long_summary(self, text: str) -> Optional[str]:
        """Generate Coles-Notes-style study notes, section by section.

        A single call asking for the whole episode in 500-800 words gets
        compressed down to a couple of paragraphs regardless of how the
        instruction is worded — a model that can summarize the whole thing
        in one paragraph will. Splitting the transcript into sections and
        asking for real notes on each one removes that option, and scales
        naturally: a longer episode gets more sections and longer notes.
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

        key_topics = [word for word, _ in
                     sorted(word_freq.items(), key=lambda x: x[1], reverse=True)[:5]]

        # Simple company extraction (words ending in common suffixes)
        potential_companies = set()
        for word in text.split():
            if any(word.lower().endswith(suffix) for suffix in ['inc', 'corp', 'llc', 'labs']):
                potential_companies.add(word)

        return {
            "key_topics": key_topics,
            "themes": ["general discussion"],
            "quotes": [],
            "startups": list(potential_companies),
            "summary": "Podcast episode discussion covering various topics."
        }

    def process_all_transcribed(self) -> int:
        """Process all episodes with 'transcribed' status."""
        episodes = self.db.get_episodes_by_status('transcribed')
        processed_count = 0

        for episode in episodes:
            logger.info("Processing summary for: %s", episode['title'])
            if self.generate_summary(episode['id']):
                processed_count += 1
                logger.info("Processed: %s", episode['title'])
            else:
                logger.warning("Failed to process: %s", episode['title'])

        return processed_count
