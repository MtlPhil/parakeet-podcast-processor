"""Job wrappers around the P3 pipeline modules.

Each task takes a job_id and records status and progress in the jobs table.
Tasks are executed one at a time by the serial runner in job_queue.py.
Pipeline modules are imported inside each task so the API starts quickly
without loading ML dependencies.
"""

import logging
import threading
import time
from datetime import datetime
from typing import Optional

from p3.api.deps import get_db, load_config
from p3.api.job_queue import job_runner
from p3.llm import DEFAULT_OLLAMA_URL, resolve_provider_and_model

logger = logging.getLogger(__name__)


def _get_settings() -> dict:
    config = load_config()
    return config.get("settings", {})


def _make_transcriber(db, settings: dict):
    from p3.transcriber import DEFAULT_PARAKEET_MODEL, AudioTranscriber

    return AudioTranscriber(
        db=db,
        whisper_model=settings.get("whisper_model", "base"),
        use_parakeet=settings.get("parakeet_enabled", False),
        parakeet_model=settings.get("parakeet_model", DEFAULT_PARAKEET_MODEL),
    )


def _make_cleaner(
    db, settings: dict, provider: Optional[str] = None, model: Optional[str] = None
):
    from p3.cleaner import TranscriptCleaner

    llm_provider, llm_model = resolve_provider_and_model(settings, provider, model)
    return TranscriptCleaner(
        db=db,
        llm_provider=llm_provider,
        llm_model=llm_model,
        ollama_base_url=settings.get("ollama_base_url", DEFAULT_OLLAMA_URL),
    )


def _make_writer(
    db,
    settings: dict,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    **kwargs,
):
    from p3.writer import BlogWriter

    llm_provider, llm_model = resolve_provider_and_model(settings, provider, model)
    return BlogWriter(
        db=db,
        llm_provider=llm_provider,
        llm_model=llm_model,
        ollama_base_url=settings.get("ollama_base_url", DEFAULT_OLLAMA_URL),
        **kwargs,
    )


def _start_heartbeat(db, job_id: str, label: str, interval: float = 15.0):
    """Periodically update a running job's message with elapsed time.

    Used for steps that are a single blocking call with no progress hook
    (transcription), so the job visibly stays alive. Returns a stop()
    callable that waits for the heartbeat thread to exit, so a late tick
    never overwrites the caller's final message.
    """
    stop_event = threading.Event()
    start = time.monotonic()

    def tick():
        while not stop_event.wait(interval):
            elapsed = int(time.monotonic() - start)
            db.update_job(
                job_id, message=f"{label} ({elapsed // 60}m{elapsed % 60:02d}s elapsed)"
            )

    thread = threading.Thread(target=tick, daemon=True)
    thread.start()

    def stop():
        stop_event.set()
        thread.join(timeout=5)

    return stop


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
            db.update_job(
                job_id, status="failed", error=f"Podcast {podcast_id} not found"
            )
            return

        def on_progress(done: int, total: int, message: str):
            # Map per-episode progress onto the 5%-95% range of the bar.
            frac = done / total if total else 0
            db.update_job(job_id, progress=0.05 + frac * 0.9, message=message)

        downloader = PodcastDownloader(
            db=db,
            max_episodes=max_eps,
            audio_format=settings.get("audio_format", "wav"),
            progress_callback=on_progress,
        )

        db.update_job(
            job_id, progress=0.05, message=f"Fetching feed: {podcast['title']}"
        )
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
        episode = db.get_episode_by_id(episode_id)
        title = episode["title"] if episode else f"episode {episode_id}"

        db.update_job(
            job_id,
            status="running",
            message=f"Loading transcription model for: {title}",
        )

        transcriber = _make_transcriber(db, _get_settings())

        db.update_job(job_id, progress=0.2, message=f"Transcribing: {title}")
        stop_heartbeat = _start_heartbeat(db, job_id, f"Transcribing: {title}")
        try:
            success = transcriber.transcribe_episode(episode_id)
        finally:
            stop_heartbeat()
        transcriber.unload_models()

        if success:
            db.update_job(
                job_id,
                status="completed",
                progress=1.0,
                message=f"Transcribed: {title}",
            )
        else:
            db.update_job(
                job_id, status="failed", error="Transcription returned no result"
            )
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

        cleaner = _make_cleaner(db, _get_settings())

        db.update_job(job_id, progress=0.3, message="Cleaning transcript...")
        result = cleaner.generate_summary(episode_id)

        if result:
            db.update_job(
                job_id, status="completed", progress=1.0, message="Summary complete"
            )
        else:
            db.update_job(
                job_id, status="failed", error="Summary generation returned no result"
            )
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
            db.update_job(
                job_id, status="failed", error=f"No summaries for {target_date}"
            )
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


def task_write_blog(
    job_id: str,
    topic: str,
    target_date: str,
    target_grade: float = 91.0,
    provider: Optional[str] = None,
    model: Optional[str] = None,
):
    """Generate a blog post from podcast summaries."""
    db = get_db()
    try:
        db.update_job(job_id, status="running", message="Preparing blog generation...")

        dt = datetime.strptime(target_date, "%Y-%m-%d")
        summaries = db.get_summaries_by_date(dt)

        if not summaries:
            db.update_job(
                job_id, status="failed", error=f"No summaries for {target_date}"
            )
            return

        writer = _make_writer(
            db, _get_settings(), provider, model, target_grade=target_grade
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


def task_write_linkedin(
    job_id: str,
    episode_id: int,
    provider: Optional[str] = None,
    model: Optional[str] = None,
):
    """Generate a LinkedIn post (English + Quebec French) from one episode."""
    db = get_db()
    try:
        db.update_job(job_id, status="running", message="Preparing LinkedIn post...")

        summary = db.get_summary_by_episode(episode_id)

        if not summary:
            db.update_job(
                job_id, status="failed", error=f"No summary for episode {episode_id}"
            )
            return

        writer = _make_writer(db, _get_settings(), provider, model)

        db.update_job(job_id, progress=0.3, message="Generating LinkedIn post...")
        result = writer.generate_linkedin_post(summary)

        db.update_job(job_id, progress=0.8, message="Saving LinkedIn post...")
        file_path = writer.save_linkedin_post(result)

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
):
    """Generate the on-demand long-form synopsis for one episode."""
    db = get_db()
    try:
        db.update_job(job_id, status="running", message="Generating synopsis...")

        cleaner = _make_cleaner(db, _get_settings(), provider, model)

        synopsis = cleaner.generate_synopsis(episode_id)
        if not synopsis:
            db.update_job(
                job_id, status="failed", error="Synopsis generation returned no content"
            )
            return

        db.update_job(
            job_id, status="completed", progress=1.0, message="Synopsis generated"
        )
    except Exception as e:
        logger.exception("Synopsis generation task failed")
        db.update_job(job_id, status="failed", error=str(e))


# ------------------------------------------------------------------
# Batch queueing and the full per-episode pipeline
# ------------------------------------------------------------------


def queue_step_jobs(db, episodes, step: str) -> list[str]:
    """Create one job per eligible episode for a pipeline step and hand it to
    the serial job runner.

    step is one of 'transcribe', 'digest', 'pipeline'. Episodes whose status
    does not match the step's precondition are skipped.
    """
    plans = {
        "transcribe": (task_transcribe, "transcribe", lambda s: s == "downloaded"),
        "digest": (task_digest, "digest", lambda s: s == "transcribed"),
        "pipeline": (task_full_pipeline, "full_pipeline", lambda s: s != "processed"),
    }
    if step not in plans:
        raise ValueError(f"Unknown step: {step}")
    task_fn, job_type, eligible = plans[step]

    job_ids = []
    for ep in episodes:
        if not eligible(ep["status"]):
            continue
        job_id = db.create_job(
            job_type, episode_id=ep["id"], podcast_id=ep["podcast_id"]
        )
        job_runner.enqueue(task_fn, job_id, ep["id"])
        job_ids.append(job_id)
    return job_ids


def task_full_pipeline(job_id: str, episode_id: int):
    """Run transcribe → digest for a single episode."""
    db = get_db()
    try:
        db.update_job(job_id, status="running", message="Starting full pipeline...")

        episode = db.get_episode_by_id(episode_id)
        if not episode:
            db.update_job(
                job_id, status="failed", error=f"Episode {episode_id} not found"
            )
            return

        settings = _get_settings()

        # Step 1: Transcribe (if needed)
        if episode["status"] == "downloaded":
            db.update_job(job_id, progress=0.1, message="Transcribing...")
            transcriber = _make_transcriber(db, settings)
            success = transcriber.transcribe_episode(episode_id)
            transcriber.unload_models()
            if not success:
                db.update_job(job_id, status="failed", error="Transcription failed")
                return

        # Step 2: Digest (if needed)
        episode = db.get_episode_by_id(episode_id)
        if not episode:
            db.update_job(
                job_id, status="failed", error=f"Episode {episode_id} not found"
            )
            return
        if episode["status"] == "transcribed":
            db.update_job(job_id, progress=0.5, message="Generating summary...")
            cleaner = _make_cleaner(db, settings)
            result = cleaner.generate_summary(episode_id)
            if not result:
                db.update_job(
                    job_id, status="failed", error="Summary generation failed"
                )
                return

        db.update_job(
            job_id, status="completed", progress=1.0, message="Pipeline complete"
        )
    except Exception as e:
        logger.exception("Full pipeline task failed")
        db.update_job(job_id, status="failed", error=str(e))
