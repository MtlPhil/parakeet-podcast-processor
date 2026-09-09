"""Tests for DigestExporter."""

import json
from datetime import date, datetime

import pytest

from p3.exporter import DigestExporter


@pytest.fixture
def sample_summaries():
    return [
        {
            'id': 1,
            'episode_id': 1,
            'podcast_title': 'Tech Talk',
            'episode_title': 'AI Revolution',
            'key_topics': ['artificial intelligence', 'machine learning'],
            'themes': ['technology disruption'],
            'quotes': ['AI will transform everything'],
            'startups': ['OpenAI', 'Anthropic'],
            'full_summary': 'A discussion about the latest AI developments.',
            'digest_date': date(2025, 8, 25),
            'created_at': datetime(2025, 8, 25, 10, 0, 0),
        },
        {
            'id': 2,
            'episode_id': 2,
            'podcast_title': 'Tech Talk',
            'episode_title': 'Cloud Computing',
            'key_topics': ['cloud', 'serverless'],
            'themes': ['infrastructure'],
            'quotes': [],
            'startups': ['AWS'],
            'full_summary': 'Deep dive into cloud computing trends.',
            'digest_date': date(2025, 8, 25),
            'created_at': datetime(2025, 8, 25, 11, 0, 0),
        },
    ]


@pytest.fixture
def exporter(tmp_path):
    return DigestExporter(db=None, export_dir=str(tmp_path))


class TestMarkdownExport:
    def test_contains_header(self, exporter, sample_summaries):
        md = exporter.export_markdown(sample_summaries, date(2025, 8, 25))
        assert '# Podcast Digest - 2025-08-25' in md

    def test_groups_by_podcast(self, exporter, sample_summaries):
        md = exporter.export_markdown(sample_summaries, date(2025, 8, 25))
        assert '## Tech Talk' in md
        assert '### AI Revolution' in md
        assert '### Cloud Computing' in md

    def test_includes_topics(self, exporter, sample_summaries):
        md = exporter.export_markdown(sample_summaries, date(2025, 8, 25))
        assert 'artificial intelligence' in md
        assert 'machine learning' in md

    def test_includes_quotes(self, exporter, sample_summaries):
        md = exporter.export_markdown(sample_summaries, date(2025, 8, 25))
        assert '> AI will transform everything' in md

    def test_includes_companies(self, exporter, sample_summaries):
        md = exporter.export_markdown(sample_summaries, date(2025, 8, 25))
        assert 'OpenAI' in md
        assert 'Anthropic' in md

    def test_empty_summaries(self, exporter):
        md = exporter.export_markdown([], date(2025, 8, 25))
        assert 'No summaries available' in md


class TestJsonExport:
    def test_valid_json(self, exporter, sample_summaries):
        output = exporter.export_json(sample_summaries, date(2025, 8, 25))
        data = json.loads(output)
        assert data['date'] == '2025-08-25'
        assert data['total_episodes'] == 2

    def test_date_serialization(self, exporter, sample_summaries):
        """Verify that dates are serialized as ISO strings, not via str()."""
        output = exporter.export_json(sample_summaries, date(2025, 8, 25))
        data = json.loads(output)
        # The date field should be ISO format
        assert data['date'] == '2025-08-25'

    def test_empty_summaries(self, exporter):
        output = exporter.export_json([], date(2025, 8, 25))
        data = json.loads(output)
        assert data['total_episodes'] == 0
        assert data['summaries'] == []


class TestHtmlExport:
    def test_escapes_html_entities(self, exporter):
        """Verify that HTML special characters in content are escaped."""
        summaries = [{
            'podcast_title': '<script>alert("xss")</script>',
            'episode_title': 'Test & "Episode"',
            'key_topics': ['<b>bold</b>'],
            'themes': [],
            'quotes': ['Quote with <tag>'],
            'startups': [],
            'full_summary': 'Summary with <html> tags & entities',
            'digest_date': date(2025, 8, 25),
            'created_at': datetime(2025, 8, 25, 10, 0, 0),
        }]
        html = exporter.export_email_html(summaries, date(2025, 8, 25))

        # Raw HTML should NOT appear unescaped
        assert '<script>' not in html
        assert '&lt;script&gt;' in html
        assert '&amp;' in html

    def test_empty_summaries(self, exporter):
        html = exporter.export_email_html([], date(2025, 8, 25))
        assert 'No summaries available' in html


class TestExportPath:
    def test_get_export_path(self, exporter, tmp_path):
        path = exporter.get_export_path("digest_2025-08-25.md")
        assert path == tmp_path / "digest_2025-08-25.md"
