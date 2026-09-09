"""Export functionality for P³ digests."""

import json
import logging
from datetime import date, datetime
from html import escape
from pathlib import Path
from typing import Dict, List, Any

logger = logging.getLogger(__name__)

# Default export directory (created by `p3 init`)
DEFAULT_EXPORT_DIR = Path("exports")


class DigestExporter:
    def __init__(self, db, export_dir: str = None):
        self.db = db
        self.export_dir = Path(export_dir) if export_dir else DEFAULT_EXPORT_DIR
        self.export_dir.mkdir(parents=True, exist_ok=True)

    def get_export_path(self, filename: str) -> Path:
        """Return a path inside the export directory."""
        return self.export_dir / filename

    def export_markdown(self, summaries: List[Dict[str, Any]], target_date: date) -> str:
        """Export summaries as Markdown."""
        content = [f"# Podcast Digest - {target_date}\n"]

        if not summaries:
            content.append("No summaries available for this date.\n")
            return "\n".join(content)

        # Group by podcast
        by_podcast: Dict[str, List[Dict[str, Any]]] = {}
        for summary in summaries:
            podcast = summary['podcast_title']
            if podcast not in by_podcast:
                by_podcast[podcast] = []
            by_podcast[podcast].append(summary)

        for podcast_name, episodes in by_podcast.items():
            content.append(f"## {podcast_name}\n")

            for episode in episodes:
                content.append(f"### {episode['episode_title']}\n")

                if episode['full_summary']:
                    content.append(f"**Summary:** {episode['full_summary']}\n")

                if episode['key_topics']:
                    content.append("**Key Topics:**")
                    for topic in episode['key_topics']:
                        content.append(f"- {topic}")
                    content.append("")

                if episode['themes']:
                    content.append("**Themes:**")
                    for theme in episode['themes']:
                        content.append(f"- {theme}")
                    content.append("")

                if episode['quotes']:
                    content.append("**Notable Quotes:**")
                    for quote in episode['quotes']:
                        content.append(f"> {quote}")
                    content.append("")

                if episode['startups']:
                    content.append("**Companies/Startups Mentioned:**")
                    for startup in episode['startups']:
                        content.append(f"- {startup}")
                    content.append("")

                content.append("---\n")

        return "\n".join(content)

    def export_json(self, summaries: List[Dict[str, Any]], target_date: date) -> str:
        """Export summaries as JSON with proper type handling."""

        def _serialize(obj):
            """Convert non-serializable types explicitly."""
            if isinstance(obj, (datetime, date)):
                return obj.isoformat()
            raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

        export_data = {
            "date": target_date.isoformat(),
            "total_episodes": len(summaries),
            "summaries": summaries
        }

        return json.dumps(export_data, indent=2, default=_serialize)

    def export_email_html(self, summaries: List[Dict[str, Any]], target_date: date) -> str:
        """Export summaries as HTML for email. All user-supplied content is escaped."""
        html = f"""
        <html>
        <head>
            <title>Podcast Digest - {escape(str(target_date))}</title>
            <style>
                body {{ font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; }}
                h1, h2, h3 {{ color: #333; }}
                .podcast {{ margin-bottom: 2em; }}
                .episode {{ margin-bottom: 1.5em; padding: 1em; background: #f9f9f9; }}
                .summary {{ font-style: italic; margin-bottom: 1em; }}
                .topics, .themes, .startups {{ margin-bottom: 0.5em; }}
                .quote {{ background: #e8e8e8; padding: 0.5em; margin: 0.5em 0; }}
                ul {{ margin: 0.5em 0; }}
            </style>
        </head>
        <body>
            <h1>Podcast Digest - {escape(str(target_date))}</h1>
        """

        if not summaries:
            html += "<p>No summaries available for this date.</p>"
        else:
            by_podcast: Dict[str, List[Dict[str, Any]]] = {}
            for summary in summaries:
                podcast = summary['podcast_title']
                if podcast not in by_podcast:
                    by_podcast[podcast] = []
                by_podcast[podcast].append(summary)

            for podcast_name, episodes in by_podcast.items():
                html += f'<div class="podcast"><h2>{escape(podcast_name)}</h2>'

                for episode in episodes:
                    html += f'<div class="episode"><h3>{escape(episode["episode_title"])}</h3>'

                    if episode['full_summary']:
                        html += f'<div class="summary"><strong>Summary:</strong> {escape(episode["full_summary"])}</div>'

                    if episode['key_topics']:
                        html += '<div class="topics"><strong>Key Topics:</strong><ul>'
                        for topic in episode['key_topics']:
                            html += f'<li>{escape(str(topic))}</li>'
                        html += '</ul></div>'

                    if episode['themes']:
                        html += '<div class="themes"><strong>Themes:</strong><ul>'
                        for theme in episode['themes']:
                            html += f'<li>{escape(str(theme))}</li>'
                        html += '</ul></div>'

                    if episode['quotes']:
                        html += '<div><strong>Notable Quotes:</strong>'
                        for quote in episode['quotes']:
                            html += f'<div class="quote">{escape(str(quote))}</div>'
                        html += '</div>'

                    if episode['startups']:
                        html += '<div class="startups"><strong>Companies/Startups:</strong><ul>'
                        for startup in episode['startups']:
                            html += f'<li>{escape(str(startup))}</li>'
                        html += '</ul></div>'

                    html += '</div>'  # episode

                html += '</div>'  # podcast

        html += """
        </body>
        </html>
        """

        return html
