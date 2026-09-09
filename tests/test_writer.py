"""Tests for writer utilities (non-LLM paths)."""

import pytest

from p3.writer import BlogWriter


class TestParseGrade:
    def test_standard_format(self):
        response = "GRADE: A-\nSCORE: 91\nFEEDBACK: Great work overall."
        writer = BlogWriter(db=None)
        result = writer._parse_grade(response)
        assert result['grade'] == 'A-'
        assert result['score'] == 91.0
        assert 'Great work overall' in result['feedback']

    def test_case_insensitive(self):
        response = "grade: B+\nscore: 85\nfeedback: Decent effort."
        writer = BlogWriter(db=None)
        result = writer._parse_grade(response)
        assert result['grade'] == 'B+'
        assert result['score'] == 85.0

    def test_unparseable_defaults_to_zero(self):
        response = "This blog post is mediocre. I'd give it a B."
        writer = BlogWriter(db=None)
        result = writer._parse_grade(response)
        assert result['score'] == 0.0
        assert result['grade'] == '?'

    def test_decimal_score(self):
        response = "GRADE: A\nSCORE: 92.5\nFEEDBACK: Almost perfect."
        writer = BlogWriter(db=None)
        result = writer._parse_grade(response)
        assert result['score'] == 92.5


class TestParseNumberedList:
    def test_dot_format(self):
        text = "1. First item\n2. Second item\n3. Third item"
        result = BlogWriter._parse_numbered_list(text)
        assert len(result) == 3
        assert result[0] == "First item"
        assert result[2] == "Third item"

    def test_paren_format(self):
        text = "1) First item\n2) Second item"
        result = BlogWriter._parse_numbered_list(text)
        assert len(result) == 2

    def test_post_format(self):
        text = "POST 1: First post\nPOST 2: Second post\nPOST 3: Third post"
        result = BlogWriter._parse_numbered_list(text)
        assert len(result) == 3

    def test_empty_input(self):
        result = BlogWriter._parse_numbered_list("")
        assert result == []


class TestGenerateSlug:
    def test_basic_slug(self):
        assert BlogWriter._generate_slug("AI's Impact on Software") == "ais-impact-on-software"

    def test_special_characters(self):
        assert BlogWriter._generate_slug("Hello, World! #1") == "hello-world-1"

    def test_extra_spaces(self):
        assert BlogWriter._generate_slug("  too   many  spaces  ") == "too-many-spaces"


class TestBuildContext:
    def test_single_summary(self):
        writer = BlogWriter(db=None)
        summaries = [{
            'episode_title': 'Episode 1',
            'podcast_title': 'Podcast A',
            'full_summary': 'Summary text',
            'key_topics': ['AI'],
            'themes': ['tech'],
            'quotes': ['quote1'],
            'startups': ['StartupX'],
        }]
        context = writer._build_context(summaries)
        assert 'Episode 1' in context
        assert 'Podcast A' in context
        assert 'AI' in context

    def test_multiple_summaries(self):
        writer = BlogWriter(db=None)
        summaries = [
            {
                'episode_title': f'Ep {i}',
                'podcast_title': f'Pod {i}',
                'full_summary': f'Summary {i}',
                'key_topics': [f'topic{i}'],
                'themes': [],
                'quotes': [],
                'startups': [],
            }
            for i in range(3)
        ]
        context = writer._build_context(summaries)
        assert 'Source 1' in context
        assert 'Source 2' in context
        assert 'Source 3' in context
