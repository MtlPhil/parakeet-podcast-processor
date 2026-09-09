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
_MAX_TRANSCRIPT_CHARS = 60_000  # ~15k tokens, safe for most model context windows

_CLEAN_PROMPT = """Clean this podcast transcript by:
1. Removing filler words (um, uh, like, you know)
2. Fixing grammar and punctuation
3. Preserving technical terms and proper nouns exactly
4. Maintaining the speaker's voice and meaning
5. Breaking into clear paragraphs

Return only the cleaned text, no additional commentary.

Transcript:
"""

_SUMMARY_PROMPT = """Analyze this podcast transcript and extract structured information in JSON format:

{
  "key_topics": ["topic1", "topic2", ...],
  "themes": ["theme1", "theme2", ...],
  "quotes": ["notable quote 1", "notable quote 2", ...],
  "startups": ["company1", "company2", ...],
  "summary": "Brief 2-3 sentence summary"
}

Guidelines:
- key_topics: Main subjects discussed (3-5 topics)
- themes: Broader themes or patterns (2-4 themes)
- quotes: Memorable, insightful quotes (2-3 max)
- startups: Any companies, startups, or brands mentioned
- summary: Concise overview of the episode

Transcript:
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

    def _chat_openai(self, system: str, user: str) -> str:
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
                    "max_tokens": 2000
                }
            )
        if response.status_code != 200:
            raise RuntimeError(f"OpenAI API error: {response.status_code} - {response.text}")
        return response.json()["choices"][0]["message"]["content"].strip()

    def _chat_ollama(self, system: str, user: str) -> str:
        """Send a chat completion request to Ollama."""
        if not OLLAMA_AVAILABLE:
            raise RuntimeError("Ollama Python package is not installed")
        response = ollama.chat(
            model=self.llm_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ]
        )
        return response['message']['content'].strip()

    def _chat(self, system: str, user: str) -> str:
        """Route a chat request to the configured LLM provider."""
        if self.llm_provider == "openai":
            return self._chat_openai(system, user)
        elif self.llm_provider == "ollama":
            return self._chat_ollama(system, user)
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
                    _CLEAN_PROMPT + truncated
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

        if summary_data:
            self.db.add_summary(
                episode_id=episode_id,
                key_topics=summary_data.get('key_topics', []),
                themes=summary_data.get('themes', []),
                quotes=summary_data.get('quotes', []),
                startups=summary_data.get('startups', []),
                full_summary=summary_data.get('summary', ''),
                digest_date=datetime.now()
            )
            self.db.update_episode_status(episode_id, 'processed')

        return summary_data

    def _generate_structured_summary(self, text: str) -> Optional[Dict[str, Any]]:
        """Generate structured summary using LLM."""
        if not self.api_key and self.llm_provider != "ollama":
            return self._basic_extraction(text)

        truncated = _truncate_transcript(text)

        try:
            response = self._chat(
                "You are an expert at analyzing podcast content. Return valid JSON only.",
                _SUMMARY_PROMPT + truncated
            )
            result = _extract_json(response)
            if result is None:
                logger.warning("Could not parse JSON from LLM response, falling back to basic extraction")
                return self._basic_extraction(text)
            return result
        except Exception as e:
            logger.error("LLM summarization failed: %s", e)
            return self._basic_extraction(text)

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
