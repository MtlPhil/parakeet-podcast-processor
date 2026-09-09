"""Tests for cleaner utilities (non-LLM paths)."""

import json

import pytest

from p3.cleaner import TranscriptCleaner, _extract_json, _truncate_transcript


class TestTruncateTranscript:
    def test_short_text_unchanged(self):
        text = "Short text"
        assert _truncate_transcript(text) == text

    def test_long_text_truncated(self):
        text = "A" * 100_000
        result = _truncate_transcript(text, max_chars=1000)
        assert len(result) < 100_000
        assert "[... transcript truncated for length ...]" in result

    def test_preserves_start_and_end(self):
        text = "START" + "x" * 100_000 + "END"
        result = _truncate_transcript(text, max_chars=2000)
        assert result.startswith("START")
        assert result.endswith("END")


class TestExtractJson:
    def test_simple_json(self):
        text = '{"key": "value"}'
        assert _extract_json(text) == {"key": "value"}

    def test_json_with_surrounding_text(self):
        text = 'Here is the result: {"key": "value"} Hope that helps!'
        assert _extract_json(text) == {"key": "value"}

    def test_json_in_code_fence(self):
        text = '```json\n{"key": "value"}\n```'
        assert _extract_json(text) == {"key": "value"}

    def test_nested_json(self):
        data = {"outer": {"inner": [1, 2, 3]}}
        text = f"Result: {json.dumps(data)}"
        assert _extract_json(text) == data

    def test_no_json(self):
        assert _extract_json("no json here") is None

    def test_invalid_json(self):
        assert _extract_json("{invalid json}") is None


class TestBasicExtraction:
    def test_returns_expected_keys(self):
        cleaner = TranscriptCleaner(db=None, llm_provider="ollama")
        result = cleaner._basic_extraction("This is a test with some words about technology and innovation")
        assert 'key_topics' in result
        assert 'themes' in result
        assert 'quotes' in result
        assert 'startups' in result
        assert 'summary' in result

    def test_extracts_frequent_words(self):
        text = "technology " * 20 + "innovation " * 15 + "startup " * 10
        cleaner = TranscriptCleaner(db=None, llm_provider="ollama")
        result = cleaner._basic_extraction(text)
        assert 'technology' in result['key_topics']

    def test_extracts_company_suffixes(self):
        text = "We spoke with representatives from AcmeCorp and InnovateLabs about their products."
        cleaner = TranscriptCleaner(db=None, llm_provider="ollama")
        result = cleaner._basic_extraction(text)
        assert any('AcmeCorp' in s for s in result['startups']) or \
               any('InnovateLabs' in s for s in result['startups'])


class TestCleanTranscript:
    def test_removes_filler_words(self):
        cleaner = TranscriptCleaner(db=None, llm_provider="ollama")
        # Without LLM (ollama not available in tests), just regex cleaning
        text = "So um we uh talked about er the technology hmm today"
        result = cleaner.clean_transcript(text)
        assert "um" not in result.split()
        assert "uh" not in result.split()
        assert "er" not in result.split()
        assert "hmm" not in result.split()

    def test_preserves_meaningful_words(self):
        """Words like 'actually', 'basically' should be preserved."""
        cleaner = TranscriptCleaner(db=None, llm_provider="ollama")
        text = "This actually works and is basically correct"
        result = cleaner.clean_transcript(text)
        assert "actually" in result
        assert "basically" in result
