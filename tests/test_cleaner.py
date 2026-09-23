"""Tests for cleaner utilities (non-LLM paths)."""

import json
from datetime import datetime

import pytest

from p3.cleaner import (
    TranscriptCleaner,
    _coerce_str_list,
    _extract_json,
    _split_into_chunks,
    _split_into_sections,
    _split_segments_by_gaps,
    _strip_llm_meta_lines,
    _truncate_chunks,
    _truncate_transcript,
)
from p3.database import P3Database


def _seg(text, start, end):
    return {"text": text, "timestamp_start": start, "timestamp_end": end}


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
        result = cleaner._basic_extraction(
            "This is a test with some words about technology and innovation"
        )
        assert "key_topics" in result
        assert "themes" in result
        assert "quotes" in result
        assert "startups" in result
        assert "summary" in result

    def test_extracts_frequent_words(self):
        text = "technology " * 20 + "innovation " * 15 + "startup " * 10
        cleaner = TranscriptCleaner(db=None, llm_provider="ollama")
        result = cleaner._basic_extraction(text)
        assert "technology" in result["key_topics"]

    def test_extracts_company_suffixes(self):
        text = "We spoke with representatives from AcmeCorp and InnovateLabs about their products."
        cleaner = TranscriptCleaner(db=None, llm_provider="ollama")
        result = cleaner._basic_extraction(text)
        assert any("AcmeCorp" in s for s in result["startups"]) or any(
            "InnovateLabs" in s for s in result["startups"]
        )


class TestCleanTranscript:
    @pytest.fixture(autouse=True)
    def _no_llm(self, monkeypatch):
        # An OpenAI provider without a key has no usable LLM, so only the
        # regex cleaning runs and no network calls are made.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def test_removes_filler_words(self):
        cleaner = TranscriptCleaner(db=None, llm_provider="openai")
        text = "So um we uh talked about er the technology hmm today"
        result = cleaner.clean_transcript(text)
        assert "um" not in result.split()
        assert "uh" not in result.split()
        assert "er" not in result.split()
        assert "hmm" not in result.split()

    def test_preserves_meaningful_words(self):
        """Words like 'actually', 'basically' should be preserved."""
        cleaner = TranscriptCleaner(db=None, llm_provider="openai")
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
        assert _coerce_str_list(items) == [
            "Informed Analysis",
            "Book Movement Detection",
        ]

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


class TestSplitIntoChunks:
    def test_never_splits_mid_sentence(self):
        text = "First sentence here. Second sentence here. Third sentence here."
        chunks = _split_into_chunks(text, max_chars=30)
        for chunk in chunks:
            assert chunk.strip().endswith((".", "!", "?"))

    def test_packs_sentences_up_to_max_chars(self):
        text = "One. Two. Three. Four. Five."
        chunks = _split_into_chunks(text, max_chars=1000)
        assert chunks == ["One. Two. Three. Four. Five."]

    def test_reconstructs_full_content(self):
        text = "Alpha bravo charlie. Delta echo foxtrot. Golf hotel india. Juliet kilo lima."
        chunks = _split_into_chunks(text, max_chars=25)
        rejoined = " ".join(chunks)
        for word in text.replace(".", "").split():
            assert word in rejoined

    def test_single_sentence_longer_than_max_chars_kept_whole(self):
        sentence = (
            "This is one very long sentence with no other punctuation in it at all"
        )
        chunks = _split_into_chunks(sentence, max_chars=10)
        assert chunks == [sentence]

    def test_empty_text_returns_empty_list(self):
        assert _split_into_chunks("") == []
        assert _split_into_chunks("   ") == []


class TestStripLlmMetaLines:
    def test_strips_here_is_preamble(self):
        text = "Here is the cleaned transcript:\n\nActual content here."
        assert _strip_llm_meta_lines(text) == "Actual content here."

    def test_strips_heres_the_cleaned_podcast_transcript(self):
        text = "Here's the cleaned podcast transcript:\nSome content."
        assert _strip_llm_meta_lines(text) == "Some content."

    def test_strips_rephrasing_note(self):
        text = "Some content.\nNote: some minor rephrasing was done for clarity."
        assert _strip_llm_meta_lines(text) == "Some content."

    def test_leaves_normal_content_untouched(self):
        text = "This is just normal transcript content, nothing to strip."
        assert _strip_llm_meta_lines(text) == text


class TestSplitSegmentsByGaps:
    def test_splits_on_large_gap(self):
        segments = [
            _seg("Talking about topic one here.", 0.0, 2.0),
            _seg("Still on topic one.", 2.1, 4.0),
            _seg("Now an ad break starts.", 10.0, 12.0),
            _seg("End of the ad break.", 12.1, 14.0),
        ]
        chunks = _split_segments_by_gaps(segments, min_gap=0.5, min_chars=0)
        assert len(chunks) == 2
        assert "topic one" in chunks[0]
        assert "ad break" in chunks[1]

    def test_no_gaps_produces_single_chunk(self):
        segments = [
            _seg("First part.", 0.0, 2.0),
            _seg("Second part.", 2.05, 4.0),
            _seg("Third part.", 4.05, 6.0),
        ]
        chunks = _split_segments_by_gaps(segments, min_gap=0.5, min_chars=0)
        assert len(chunks) == 1
        assert "First part" in chunks[0]
        assert "Third part" in chunks[0]

    def test_small_fragments_merged_into_neighbor(self):
        segments = [
            _seg(
                "A fairly long opening segment about the main topic being discussed.",
                0.0,
                2.0,
            ),
            _seg("Hi.", 10.0, 10.5),
            _seg(
                "Another fairly long segment continuing the conversation afterward.",
                20.0,
                22.0,
            ),
        ]
        chunks = _split_segments_by_gaps(segments, min_gap=0.5, min_chars=20)
        assert len(chunks) == 2
        assert "Hi." in chunks[0] or "Hi." in chunks[1]

    def test_long_gap_free_run_still_splits_on_max_chars(self):
        long_text = "Sentence number one. " * 500
        segments = [_seg(long_text, 0.0, 100.0)]
        chunks = _split_segments_by_gaps(segments, max_chars=1000, min_chars=0)
        assert len(chunks) > 1
        assert all(len(c) <= 1000 or " " not in c for c in chunks)

    def test_empty_segments_returns_empty_list(self):
        assert _split_segments_by_gaps([]) == []


class TestTruncateChunks:
    def test_short_total_unchanged(self):
        chunks = ["one", "two", "three"]
        assert _truncate_chunks(chunks, max_chars=1000) == chunks

    def test_long_total_keeps_head_and_tail(self):
        chunks = ["a" * 100, "b" * 100, "c" * 100, "d" * 100, "e" * 100]
        result = _truncate_chunks(chunks, max_chars=250)
        assert result[0] == "a" * 100
        assert result[-1] == "e" * 100
        assert "[... transcript truncated for length ...]" in result


class TestGenerateSummaryPreservesSynopsis:
    def test_redigest_does_not_wipe_existing_long_summary(self, tmp_path, monkeypatch):
        """generate_summary is the automatic pipeline step and no longer
        touches long_summary — a redigest (e.g. to fix a themes bug) must
        not silently delete a synopsis generated separately, on demand."""
        db_path = str(tmp_path / "test.duckdb")
        db = P3Database(db_path)
        try:
            pid = db.add_podcast("Pod", "http://example.com/rss")
            now = datetime.now()
            eid = db.add_episode(pid, "Ep 1", now, "http://example.com/ep1.mp3")
            db.add_transcript_segments(
                eid,
                [
                    {
                        "text": "Some transcript content.",
                        "start": 0.0,
                        "end": 2.0,
                        "speaker": None,
                        "confidence": 1.0,
                    },
                ],
            )
            db.add_summary(
                episode_id=eid,
                key_topics=["a"],
                themes=["b"],
                quotes=[],
                startups=[],
                full_summary="old summary",
                long_summary="a hand-generated synopsis",
                digest_date=now,
            )

            # llm_provider="openai" with no api_key falls back to
            # _basic_extraction — deterministic, no network/LLM calls.
            monkeypatch.delenv("OPENAI_API_KEY", raising=False)
            cleaner = TranscriptCleaner(db=db, llm_provider="openai")
            cleaner.generate_summary(eid)

            summary = db.get_summary_by_episode(eid)
            assert summary["long_summary"] == "a hand-generated synopsis"
        finally:
            db.close()
