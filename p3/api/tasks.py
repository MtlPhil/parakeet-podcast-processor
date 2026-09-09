"""Background task wrappers around P3 pipeline modules.

Each task function accepts a job_id and updates progress in the jobs table.
These run in FastAPI BackgroundTasks (thread pool).
"""

import logging
import traceback
from datetime import datetime
from pathlib import Path

from p3.api.deps import get_db, load_config
from p3.database import P3Database

logger = logging.getLogger(__name__)


def _get_settings() -> dict:
    config = load_config()
    return config.get("settings", {})


# ------------------------------------------------------------------
# Fetch episodes for a podcast
# ------------------------------------------------------------------

def task_fetch(job_id: str, podcast_id: int, max_episodes: int | None = None):
    """Download new episodes from a podcast's RSS feed."""
    db = get_db()
    try:
        db.update_job(job_id, status="running", message="Starting fetch...")

        from p3.downloader import PodcastDownloader

        settings = _get_settings()
        max_eps = max_episodes or settings.get("max_episodes_per_feed", 10)

        podcast = db.get_podcast_by_id(podcast_id)
        if not podcast:
            db.update_job(job_id, status="failed", error=f"Podcast {podcast_id} not found")
            return

        downloader = PodcastDownloader(
            db=db,
            max_episodes=max_eps,
            audio_format=settings.get("audio_format", "wav"),
        )

        db.update_job(job_id, progress=0.1, message=f"Fetching feed: {podcast['title']}")
        count = downloader.process_feed(podcast["rss_url"])

        db.update_job(
            job_id,
            status="completed",
            progress=1.0,
            message=f"Downloaded {count} new episodes",
        )
    except Exception as e:
        logger.exception("Fetch task failed")
        db.update_job(job_id, status="failed", error=str(e))


# ------------------------------------------------------------------
# Transcribe an episode
# ------------------------------------------------------------------

def task_transcribe(job_id: str, episode_id: int):
    """Transcribe a single episode."""
    db = get_db()
    try:
        db.update_job(job_id, status="running", message="Loading transcription model...")

        from p3.transcriber import AudioTranscriber

        settings = _get_settings()
        transcriber = AudioTranscriber(
            db=db,
            whisper_model=settings.get("whisper_model", "base"),
            use_parakeet=settings.get("parakeet_enabled", False),
            parakeet_model=settings.get("parakeet_model", "mlx-community/parakeet-tdt-0.6b-v2"),
        )

        db.update_job(job_id, progress=0.2, message="Transcribing audio...")
        success = transcriber.transcribe_episode(episode_id)
        transcriber.unload_models()

        if success:
            db.update_job(job_id, status="completed", progress=1.0, message="Transcription complete")
        else:
            db.update_job(job_id, status="failed", error="Transcription returned no result")
    except Exception as e:
        logger.exception("Transcribe task failed")
        db.update_job(job_id, status="failed", error=str(e))


# ------------------------------------------------------------------
# Digest (summarize) an episode
# ------------------------------------------------------------------

def task_digest(job_id: str, episode_id: int):
    """Generate structured summary for an episode."""
    db = get_db()
    try:
        db.update_job(job_id, status="running", message="Generating summary...")

        from p3.cleaner import TranscriptCleaner

        settings = _get_settings()
        cleaner = TranscriptCleaner(
            db=db,
            llm_provider=settings.get("llm_provider", "ollama"),
            llm_model=settings.get("llm_model", "llama3.2:latest"),
            ollama_base_url=settings.get("ollama_base_url", "http://localhost:11434"),
        )

        db.update_job(job_id, progress=0.3, message="Cleaning transcript...")
        result = cleaner.generate_summary(episode_id)

        if result:
            db.update_job(job_id, status="completed", progress=1.0, message="Summary complete")
        else:
            db.update_job(job_id, status="failed", error="Summary generation returned no result")
    except Exception as e:
        logger.exception("Digest task failed")
        db.update_job(job_id, status="failed", error=str(e))


# ------------------------------------------------------------------
# Export digest for a date
# ------------------------------------------------------------------

def task_export(job_id: str, target_date: str, formats: list[str] | None = None):
    """Generate export files for a date."""
    db = get_db()
    try:
        db.update_job(job_id, status="running", message="Exporting...")

        from p3.exporter import DigestExporter

        dt = datetime.strptime(target_date, "%Y-%m-%d")
        summaries = db.get_summaries_by_date(dt)

        if not summaries:
            db.update_job(job_id, status="failed", error=f"No summaries for {target_date}")
            return

        exporter = DigestExporter(db)
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
            with open(path, "w") as f:
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

def task_write_blog(job_id: str, topic: str, target_date: str, target_grade: float = 91.0):
    """Generate a blog post from podcast summaries."""
    db = get_db()
    try:
        db.update_job(job_id, status="running", message="Preparing blog generation...")

        from p3.writer import BlogWriter

        settings = _get_settings()
        dt = datetime.strptime(target_date, "%Y-%m-%d")
        summaries = db.get_summaries_by_date(dt)

        if not summaries:
            db.update_job(job_id, status="failed", error=f"No summaries for {target_date}")
            return

        writer = BlogWriter(
            db=db,
            llm_provider=settings.get("llm_provider", "ollama"),
            llm_model=settings.get("llm_model", "llama3.2:latest"),
            target_grade=target_grade,
        )

        db.update_job(job_id, progress=0.2, message="Generating blog post...")
        blog_result = writer.generate_blog_post_from_digest(topic, summaries)

        db.update_job(job_id, progress=0.8, message="Saving blog post...")
        file_path = writer.save_blog_post(blog_result)

        db.update_job(
            job_id,
            status="completed",
            progress=1.0,
            message=f"Blog saved: {file_path} (Grade: {blog_result['final_grade']})",
        )
    except Exception as e:
        logger.exception("Blog write task failed")
        db.update_job(job_id, status="failed", error=str(e))


# ------------------------------------------------------------------
# Full pipeline for an episode
# ------------------------------------------------------------------

def task_full_pipeline(job_id: str, episode_id: int):
    """Run transcribe → digest for a single episode."""
    db = get_db()
    try:
        db.update_job(job_id, status="running", message="Starting full pipeline...")

        episode = db.get_episode_by_id(episode_id)
        if not episode:
            db.update_job(job_id, status="failed", error=f"Episode {episode_id} not found")
            return

        settings = _get_settings()

        # Step 1: Transcribe (if needed)
        if episode["status"] == "downloaded":
            db.update_job(job_id, progress=0.1, message="Transcribing...")
            from p3.transcriber import AudioTranscriber

            transcriber = AudioTranscriber(
                db=db,
                whisper_model=settings.get("whisper_model", "base"),
                use_parakeet=settings.get("parakeet_enabled", False),
                parakeet_model=settings.get("parakeet_model", "mlx-community/parakeet-tdt-0.6b-v2"),
            )
            success = transcriber.transcribe_episode(episode_id)
            transcriber.unload_models()
            if not success:
                db.update_job(job_id, status="failed", error="Transcription failed")
                return

        # Step 2: Digest (if needed)
        episode = db.get_episode_by_id(episode_id)
        if episode["status"] == "transcribed":
            db.update_job(job_id, progress=0.5, message="Generating summary...")
            from p3.cleaner import TranscriptCleaner

            cleaner = TranscriptCleaner(
                db=db,
                llm_provider=settings.get("llm_provider", "ollama"),
                llm_model=settings.get("llm_model", "llama3.2:latest"),
                ollama_base_url=settings.get("ollama_base_url", "http://localhost:11434"),
            )
            result = cleaner.generate_summary(episode_id)
            if not result:
                db.update_job(job_id, status="failed", error="Summary generation failed")
                return

        db.update_job(job_id, status="completed", progress=1.0, message="Pipeline complete")
    except Exception as e:
        logger.exception("Full pipeline task failed")
        db.update_job(job_id, status="failed", error=str(e))
