"""Content jobs: digest exports, blog posts, LinkedIn posts and
synopses, written for one user."""

import logging
from datetime import datetime
from typing import Optional

from p3.api.deps import get_db, user_settings
from p3.api.tasks.common import _make_cleaner
from p3.llm import resolve_provider_and_model
from p3.paths import content_dir
from p3.settings import setting

logger = logging.getLogger(__name__)


def _make_writer(
    db,
    settings: dict,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    **kwargs,
):
    """A writer with the LLM and tone of ``settings`` (a user's settings,
    see deps.user_settings)."""
    from p3.writer import BlogWriter

    llm_provider, llm_model = resolve_provider_and_model(settings, provider, model)
    return BlogWriter(
        db=db,
        llm_provider=llm_provider,
        llm_model=llm_model,
        ollama_base_url=setting(settings, "ollama_base_url"),
        tone=settings.get("tone") or "",
        language=settings.get("language") or "en",
        **kwargs,
    )


# ------------------------------------------------------------------
# Export digest for a date
# ------------------------------------------------------------------


def task_export(
    job_id: str,
    target_date: str,
    formats: list[str] | None = None,
    username: str | None = None,
):
    """Generate export files of a day's digests: ``username``'s episodes,
    in their language, written to their exports folder."""
    db = get_db()
    try:
        db.update_job(job_id, status="running", message="Exporting...")

        from p3.exporter import DigestExporter, localized

        dt = datetime.strptime(target_date, "%Y-%m-%d")
        language = user_settings(username).get("language", "en")
        summaries = [
            localized(row, language) for row in db.get_summaries_by_date(dt, username)
        ]

        if not summaries:
            db.update_job(
                job_id, status="failed", error=f"No summaries for {target_date}"
            )
            return

        exporter = DigestExporter(db, str(content_dir("exports", username)))
        export_formats = formats or ["markdown", "json"]
        files = []

        for fmt in export_formats:
            if fmt == "markdown":
                content = exporter.export_markdown(summaries, dt.date())
                path = exporter.get_export_path(f"digest_{target_date}.md")
            elif fmt == "json":
                content = exporter.export_json(summaries, dt.date())
                path = exporter.get_export_path(f"digest_{target_date}.json")
            else:
                continue
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            files.append(str(path))

        db.update_job(
            job_id,
            status="completed",
            progress=1.0,
            message=f"Exported: {', '.join(files)}",
        )
    except Exception as e:
        logger.exception("Export task failed")
        db.update_job(job_id, status="failed", error=str(e))


# ------------------------------------------------------------------
# Generate blog post
# ------------------------------------------------------------------


def task_write_blog(
    job_id: str,
    topic: str,
    target_date: str,
    target_grade: float = 91.0,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    username: Optional[str] = None,
):
    """Generate a blog post (English and Quebec French) from the day's
    summaries of ``username``'s episodes, in their tone."""
    db = get_db()
    try:
        db.update_job(job_id, status="running", message="Preparing blog generation...")

        dt = datetime.strptime(target_date, "%Y-%m-%d")
        summaries = db.get_summaries_by_date(dt, username)

        if not summaries:
            db.update_job(
                job_id, status="failed", error=f"No summaries for {target_date}"
            )
            return

        writer = _make_writer(
            db, user_settings(username), provider, model, target_grade=target_grade
        )

        db.update_job(job_id, progress=0.2, message="Generating blog post...")
        blog_result = writer.generate_blog_post_from_digest(topic, summaries)

        db.update_job(job_id, progress=0.8, message="Saving blog post...")
        file_path = writer.save_blog_post(
            blog_result, str(content_dir("blog_posts", username))
        )

        db.update_job(
            job_id,
            status="completed",
            progress=1.0,
            message=f"Blog saved: {file_path} (Grade: {blog_result['final_grade']})",
        )
    except Exception as e:
        logger.exception("Blog write task failed")
        db.update_job(job_id, status="failed", error=str(e))


def task_write_linkedin(
    job_id: str,
    episode_id: int,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    username: Optional[str] = None,
):
    """Generate a LinkedIn post (English + Quebec French) from one episode,
    in ``username``'s tone, saved to their folder."""
    db = get_db()
    try:
        db.update_job(job_id, status="running", message="Preparing LinkedIn post...")

        summary = db.get_summary_by_episode(episode_id)

        if not summary:
            db.update_job(
                job_id, status="failed", error=f"No summary for episode {episode_id}"
            )
            return

        from p3.exporter import shown_language

        settings = user_settings(username)
        # One episode, so "original" can be its own language.
        settings["language"] = shown_language(
            settings.get("language"), summary.get("episode_language")
        )
        writer = _make_writer(db, settings, provider, model)

        db.update_job(job_id, progress=0.3, message="Generating LinkedIn post...")
        result = writer.generate_linkedin_post(summary)

        db.update_job(job_id, progress=0.8, message="Saving LinkedIn post...")
        file_path = writer.save_linkedin_post(
            result, str(content_dir("linkedin_posts", username))
        )

        db.update_job(
            job_id,
            status="completed",
            progress=1.0,
            message=f"LinkedIn post saved: {file_path}",
        )
    except Exception as e:
        logger.exception("LinkedIn write task failed")
        db.update_job(job_id, status="failed", error=str(e))


def task_generate_synopsis(
    job_id: str,
    episode_id: int,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    username: Optional[str] = None,
):
    """Generate the on-demand long-form synopsis for one episode, in English
    and Quebec French, with the requesting user's LLM. The synopsis is
    shared: everyone with the episode sees it."""
    db = get_db()
    try:
        db.update_job(job_id, status="running", message="Generating synopsis...")

        cleaner = _make_cleaner(db, user_settings(username), provider, model)
        cleaner.generate_synopsis(episode_id)
        db.update_job(
            job_id, status="completed", progress=1.0, message="Synopsis generated"
        )
    except Exception as e:
        logger.exception("Synopsis generation task failed")
        db.update_job(job_id, status="failed", error=str(e))
