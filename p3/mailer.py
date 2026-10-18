"""The daily run's email: new episodes, their digests by theme, and any errors."""

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .database import P3Database
from .exporter import localized

logger = logging.getLogger(__name__)

# Optional on-disk location for the SMTP password (gitignored). The
# SMTP_PASSWORD environment variable takes precedence.
SMTP_PASSWORD_FILE = Path("config/smtp_password.txt")

_LABELS = {
    "en": {
        "subject": "P3 daily digest",
        "new": "new",
        "problems": "problems",
        "episodes": "New episodes",
        "digests": "Digests by theme",
        "errors": "Errors",
        "none": "No new episodes today.",
        "other": "Other",
    },
    "fr": {
        "subject": "Sommaire quotidien P3",
        "new": "nouveau(x)",
        "problems": "problème(s)",
        "episodes": "Nouveaux épisodes",
        "digests": "Résumés par thème",
        "errors": "Erreurs",
        "none": "Aucun nouvel épisode aujourd'hui.",
        "other": "Autre",
    },
}


def load_smtp_password() -> Optional[str]:
    password = os.getenv("SMTP_PASSWORD")
    if not password and SMTP_PASSWORD_FILE.exists():
        password = SMTP_PASSWORD_FILE.read_text().strip()
    return password or None


def collect_run_report(db: P3Database, run_id: str, language: str) -> Dict[str, Any]:
    """What the run produced: its new episodes (with their digest in
    ``language``) and every error its jobs recorded."""
    episodes: List[Dict[str, Any]] = []
    errors: List[str] = []
    seen = set()
    for job in db.get_jobs_by_run(run_id):
        where = " — ".join(
            part for part in (job["podcast_title"], job["episode_title"]) if part
        )
        if job["error"]:
            errors.append(f"{job['job_type']} ({where or 'n/a'}): {job['error']}")
        episode_id = job["episode_id"]
        if (
            job["job_type"] != "full_pipeline"
            or job["status"] != "completed"
            or episode_id in seen
        ):
            continue
        seen.add(episode_id)
        summary = db.get_summary_by_episode(episode_id)
        if not summary:
            continue
        summary = localized(summary, language)
        themes = summary.get("themes") or []
        episodes.append(
            {
                "podcast": job["podcast_title"] or "",
                "title": job["episode_title"] or "",
                "themes": themes,
                "summary": summary.get("full_summary") or "",
            }
        )
    return {"episodes": episodes, "errors": errors}


def group_by_theme(
    episodes: List[Dict[str, Any]], other: str
) -> List[Tuple[str, List[Dict[str, Any]]]]:
    """Episodes grouped under their first theme, so each digest appears once.
    Groups with the most episodes come first; episodes without a theme go
    last under ``other``."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for episode in episodes:
        theme = str(episode["themes"][0]) if episode["themes"] else other
        groups.setdefault(theme, []).append(episode)
    return sorted(groups.items(), key=lambda kv: (kv[0] == other, -len(kv[1])))


def build_email(
    report: Dict[str, Any], day: str, language: str
) -> Tuple[str, str, str]:
    """(subject, plain text, HTML) for a run report."""
    t = _LABELS.get(language, _LABELS["en"])
    episodes, errors = report["episodes"], report["errors"]
    groups = group_by_theme(episodes, t["other"])

    subject = f"{t['subject']} — {day} ({len(episodes)} {t['new']}"
    subject += f", {len(errors)} {t['problems']})" if errors else ")"

    text = [f"{t['episodes']}", ""]
    html = [f"<h2>{escape(t['episodes'])}</h2>"]
    if episodes:
        html.append("<ul>")
        for ep in episodes:
            text.append(f"- {ep['podcast']}: {ep['title']}")
            html.append(
                f"<li><strong>{escape(ep['podcast'])}</strong>: "
                f"{escape(ep['title'])}</li>"
            )
        html.append("</ul>")
        text += ["", t["digests"], ""]
        html.append(f"<h2>{escape(t['digests'])}</h2>")
        for theme, items in groups:
            text += [theme.upper(), ""]
            html.append(f"<h3>{escape(theme)}</h3>")
            for ep in items:
                text += [f"{ep['podcast']}: {ep['title']}", ep["summary"], ""]
                html.append(f"<h4>{escape(ep['podcast'])}: {escape(ep['title'])}</h4>")
                for para in ep["summary"].split("\n\n"):
                    if para.strip():
                        html.append(f"<p>{escape(para.strip())}</p>")
    else:
        text.append(t["none"])
        html.append(f"<p>{escape(t['none'])}</p>")

    if errors:
        text += ["", t["errors"], ""] + [f"- {e}" for e in errors]
        html.append(f"<h2>{escape(t['errors'])}</h2><ul>")
        html += [f"<li>{escape(e)}</li>" for e in errors]
        html.append("</ul>")

    return subject, "\n".join(text), "".join(html)


def send_email(settings: Dict[str, Any], to: str, subject: str, text: str, html: str):
    """Send one message through the SMTP server in the shared settings.
    Raises on any failure; the caller decides what to do about it."""
    host = settings.get("smtp_host")
    if not host:
        raise RuntimeError("smtp_host is not set in config/feeds.yaml")
    user = settings.get("smtp_user")
    password = load_smtp_password()
    if user and not password:
        raise RuntimeError(
            f"No SMTP password found: set SMTP_PASSWORD or create {SMTP_PASSWORD_FILE}"
        )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.get("smtp_from") or user
    message["To"] = to
    message.set_content(text)
    message.add_alternative(html, subtype="html")

    port = int(settings.get("smtp_port") or 587)
    context = ssl.create_default_context()
    if port == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, context=context, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
    with server:
        if port != 465:
            server.starttls(context=context)
        if user:
            server.login(user, password or "")
        server.send_message(message)
