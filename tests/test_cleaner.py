"""Tests for cleaner utilities (non-LLM paths)."""

import json

import pytest

from p3.cleaner import (
    TranscriptCleaner,
    _coerce_str_list,
    _extract_json,
    _split_into_sections,
    _truncate_transcript,
)


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


class TestSplitIntoSections:
    def test_splits_into_requested_count(self):
        text = "word " * 1000
        sections = _split_into_sections(text, 4)
        assert len(sections) == 4

    def test_does_not_cut_words_in_half(self):
        text = "alpha beta gamma delta epsilon zeta eta theta"
        sections = _split_into_sections(text, 3)
        rejoined = " ".join(sections)
        for word in text.split():
            assert word in rejoined.split()

    def test_covers_the_whole_text(self):
        text = "one two three four five six seven eight nine ten"
        sections = _split_into_sections(text, 3)
        assert "".join(sections).replace(" ", "") == text.replace(" ", "")

    def test_single_section_returns_whole_text(self):
        assert _split_into_sections("hello world", 1) == ["hello world"]

    def test_empty_text_returns_empty_list(self):
        assert _split_into_sections("", 4) == []
        assert _split_into_sections("   ", 4) == []


class TestCoerceStrList:
    def test_list_of_strings_unchanged(self):
        assert _coerce_str_list(["a", "b"]) == ["a", "b"]

    def test_dict_items_use_name_field(self):
        items = [
            {"theme_name": "Informed Analysis", "description": ""},
            {"theme_name": "Book Movement Detection", "description": None},
        ]
        assert _coerce_str_list(items) == ["Informed Analysis", "Book Movement Detection"]

    def test_dict_items_without_known_key_use_first_string_value(self):
        items = [{"label": "Foo", "count": 3}]
        assert _coerce_str_list(items) == ["Foo"]

    def test_dict_items_with_no_string_value_stringified(self):
        items = [{"count": 3}]
        assert _coerce_str_list(items) == [str({"count": 3})]

    def test_non_list_returns_empty_list(self):
        assert _coerce_str_list(None) == []
        assert _coerce_str_list("not a list") == []

    def test_non_string_non_dict_items_stringified(self):
        assert _coerce_str_list([1, 2.5]) == ["1", "2.5"]
