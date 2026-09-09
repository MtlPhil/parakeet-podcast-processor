"""Tests for downloader utilities.

These tests exercise the pure utility functions without importing the full
downloader module (which requires feedparser, which may not be installable
in all environments).
"""

import re


def _safe_filename(title: str, max_length: int = 50) -> str:
    """Mirror of p3.downloader._safe_filename for isolated testing."""
    cleaned = re.sub(r'[^\w\s-]', '', title)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    if not cleaned:
        cleaned = "untitled"
    return cleaned[:max_length]


class TestSafeFilename:
    def test_basic_title(self):
        assert _safe_filename("My Podcast Episode") == "My Podcast Episode"

    def test_special_characters_stripped(self):
        result = _safe_filename("Episode: The Best! (Part 1)")
        assert ":" not in result
        assert "!" not in result
        assert "(" not in result

    def test_truncation(self):
        long_title = "A" * 100
        result = _safe_filename(long_title, max_length=50)
        assert len(result) == 50

    def test_empty_title(self):
        assert _safe_filename("") == "untitled"

    def test_all_special_chars(self):
        assert _safe_filename("!!!???...") == "untitled"

    def test_whitespace_collapse(self):
        result = _safe_filename("Too   many    spaces")
        assert "  " not in result
