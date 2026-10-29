"""Job wrappers around the P3 pipeline modules.

Each task takes a job_id and records status and progress in the jobs table.
Tasks are executed one at a time by the serial runner in job_queue.py.
Pipeline modules are imported inside each task so the API starts quickly
without loading ML dependencies.
"""

import logging
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

from p3 import model_gate
from p3.api.deps import content_dir, get_db, load_config, user_settings
from p3.api.job_queue import job_runner
from p3.database import INTERRUPTED_ERROR
from p3.llm import (
    DEFAULT_OLLAMA_URL,
    release_ollama_models,
    resolve_provider_and_model,
)

logger = logging.getLogger(__name__)


def _get_settings() -> dict:
    """The shared settings: transcription and digest run once per episode,
    whoever's library it is in."""
    config = load_config()
    return config.get("settings", {})


def _make_transcriber(db, settings: dict):
    from p3.transcriber import (
        DEFAULT_PARAKEET_MODEL,
        DEFAULT_WHISPER_MODEL,
        AudioTranscriber,
    )

    return AudioTranscriber(
        db=db,
        whisper_model=settings.get("whisper_model", DEFAULT_WHISPER_MODEL),
        use_parakeet=settings.get("parakeet_enabled", False),
        parakeet_model=settings.get("parakeet_model", DEFAULT_PARAKEET_MODEL),
        parakeet_languages=settings.get("parakeet_languages"),
    )


def _release_ollama(settings: dict) -> None:
    """Free Ollama's memory before transcription models load, unless an admin
    turned it off."""
    if settings.get("release_ollama_before_transcription", True):
        release_ollama_models(settings.get("ollama_base_url", DEFAULT_OLLAMA_URL))


# Set while transcription models are loaded (see p3/model_gate.py). Ask then
# answers with keyword search only: its models and the transcription models
# don't fit in 16 GB together.
transcribing = model_gate.transcribing


def _begin_transcription(settings: dict) -> None:
    """Stop Ask's model calls (waiting for those in flight), then free its
    reranker and Ollama's models. Call it inside the ``try`` whose
    ``finally`` calls ``model_gate.resume()``."""
    if not model_gate.pause_for_transcription():
        logger.warning("Ask model calls still running as transcription starts")
    from p3.embedder import release_ask_models

    release_ask_models()
    _release_ollama(settings)


def _make_embedder(settings: dict):
    from p3.embedder import DEFAULT_EMBEDDING_MODEL, Embedder

    return Embedder(
        settings.get("embedding_model") or DEFAULT_EMBEDDING_MODEL,
        settings.get("ollama_base_url", DEFAULT_OLLAMA_URL),
    )


def _index_episode(db, episode_id: int, settings: dict) -> str:
    """Add a digested episode to the Ask index. Never raises: when embedding
    fails the passages are kept for keyword search, and a later "Index
    library" run embeds them. Returns a note for the job message."""
    from p3.indexer import index_episode

    try:
        count = index_episode(db, episode_id, _make_embedder(settings))
        return f"{count} passages indexed"
    except Exception as e:
        logger.warning("Embedding episode %d failed: %s", episode_id, e)
    try:
        index_episode(db, episode_id, None)
        return "indexed for keyword search only (embedding failed)"
    except Exception as e:
        logger.exception("Indexing episode %d failed", episode_id)
        return f"not indexed: {e}"


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
    """A writer with the LLM and tone of ``settings`` (a user's settings,
    see deps.user_settings)."""
    from p3.writer import BlogWriter

    llm_provider, llm_model = resolve_provider_and_model(settings, provider, model)
    return BlogWriter(
        db=db,
        llm_provider=llm_provider,
        llm_model=llm_model,
        ollama_base_url=settings.get("ollama_base_url", DEFAULT_OLLAMA_URL),
        tone=settings.get("tone") or "",
        language=settings.get("language") or "en",
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


def task_fetch(
    job_id: str,
    podcast_id: int,
    max_episodes: int | None = None,
    episode_guids: list[str] | None = None,
    username: str | None = None,
):
    """Download new episodes from a source (RSS feed, YouTube channel or
    YouTube video) for ``username``: the newest ones per their settings, or
    the hand-picked ``episode_guids``. Episodes already downloaded for
    someone else are linked instead of downloaded again.
    """
    db = get_db()
    try:
        db.update_job(job_id, status="running", message="Starting fetch...")

        from p3.downloader import PodcastDownloader

        settings = user_settings(username)
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
            username=username,
        )

        db.update_job(
            job_id, progress=0.05, message=f"Fetching feed: {podcast['title']}"
        )
        count = downloader.process_feed(podcast["rss_url"], episode_guids)

        message = f"Downloaded {count} new episodes"
        if downloader.skipped:
            message += f" ({len(downloader.skipped)} skipped: deleted earlier)"
        # Problems don't fail the job (the rest of the run goes on), but they
        # are kept in ``error`` so the dashboard and the daily email show them.
        db.update_job(
            job_id,
            status="completed",
            progress=1.0,
            message=message,
            error="\n".join(downloader.problems) or None,
        )
    except Exception as e:
        logger.exception("Fetch task failed")
        db.update_job(job_id, status="failed", error=str(e))


# ------------------------------------------------------------------
# Import a YouTube playlist as one source per video
# ------------------------------------------------------------------


def task_import_playlist(
    job_id: str,
    playlist_url: str,
    category: str | None = None,
    username: str | None = None,
):
    """Create a standalone source for each video of a YouTube playlist and
    download it. Shorts and livestreams are skipped."""
    db = get_db()
    try:
        db.update_job(job_id, status="running", message="Reading playlist...")

        from p3.downloader import PodcastDownloader

        settings = _get_settings()

        def on_progress(done: int, total: int, message: str):
            frac = done / total if total else 0
            db.update_job(job_id, progress=0.05 + frac * 0.9, message=message)

        downloader = PodcastDownloader(
            db=db,
            audio_format=settings.get("audio_format", "wav"),
            progress_callback=on_progress,
            username=username,
        )
        stats = downloader.import_youtube_playlist(playlist_url, category)

        db.update_job(
            job_id,
            status="completed",
            progress=1.0,
            message=(
                f"Imported {stats['downloaded']} new videos"
                f" ({stats['existing']} already in library,"
                f" {stats['skipped']} Shorts/livestreams skipped,"
                f" {stats['failed']} failed)"
            ),
        )
    except Exception as e:
        logger.exception("Playlist import failed")
        db.update_job(job_id, status="failed", error=str(e))


# ------------------------------------------------------------------
# Add many sources at once
# ------------------------------------------------------------------


def task_bulk_add(
    job_id: str,
    urls: list[str],
    category: str | None = None,
    username: str | None = None,
):
    """Resolve and add each URL as a source to ``username``'s library, then
    queue its initial fetch (the top episodes per their settings). A source
    someone else already follows is subscribed to, not added twice. A
    playlist queues its own import job.

    Resolving is quick next to downloading, so it happens here for every URL
    and the downloads follow as separate fetch jobs, one per source.
    """
    db = get_db()
    try:
        db.update_job(job_id, status="running", message="Adding sources...")

        from p3 import youtube
        from p3.url_resolver import is_youtube_playlist, resolve_source

        added, existing, failed = 0, 0, []
        for i, url in enumerate(urls):
            db.update_job(job_id, progress=i / len(urls), message=f"Looking up: {url}")
            try:
                if is_youtube_playlist(url):
                    playlist_url = youtube.parse_youtube_url(url).url
                    import_id = db.create_job("import_playlist", username=username)
                    job_runner.enqueue(
                        task_import_playlist,
                        import_id,
                        playlist_url,
                        category,
                        username,
                    )
                    added += 1
                    continue

                source = resolve_source(url)
                podcast = db.get_podcast_by_url(source.url)
                if podcast is None and (
                    source.source_type == youtube.SOURCE_VIDEO
                    and db.episode_exists(source.url)
                ):
                    existing += 1  # already an episode of another source
                    continue
                if podcast is not None and (
                    username is None or db.is_subscribed(username, podcast["id"])
                ):
                    existing += 1
                    continue

                if podcast is None:
                    name = (
                        source.name or source.url.split("/")[-1] or "Untitled Podcast"
                    )
                    podcast_id = db.add_podcast(
                        name, source.url, category, source.source_type
                    )
                else:
                    podcast_id = podcast["id"]
                if username:
                    db.subscribe(username, podcast_id)
                fetch_id = db.create_job(
                    "fetch", podcast_id=podcast_id, username=username
                )
                job_runner.enqueue(
                    task_fetch, fetch_id, podcast_id, None, None, username
                )
                added += 1
            except (ValueError, youtube.YouTubeError) as e:
                failed.append(f"{url}: {e}")

        summary = f"Added {added}, {existing} already in library, {len(failed)} failed"
        if failed and not added and not existing:
            db.update_job(job_id, status="failed", error="; ".join(failed))
            return
        db.update_job(
            job_id,
            status="completed",
            progress=1.0,
            message=summary,
            error="; ".join(failed) if failed else None,
        )
    except Exception as e:
        logger.exception("Bulk add failed")
        db.update_job(job_id, status="failed", error=str(e))


# ------------------------------------------------------------------
# Import config-file feeds into the library
# ------------------------------------------------------------------


def task_import_feeds(job_id: str, feeds: list[dict]):
    """Add feeds.yaml entries not yet imported to the owner's library (see
    P3Database.get_owner), and fetch them. Each is imported once: a failure
    is logged, not retried."""
    db = get_db()
    try:
        db.update_job(
            job_id, status="running", message=f"Importing {len(feeds)} feed(s)..."
        )

        from p3.downloader import PodcastDownloader, mark_config_feeds_imported

        settings = _get_settings()
        downloader = PodcastDownloader(
            db=db,
            max_episodes=settings.get("max_episodes_per_feed", 10),
            audio_format=settings.get("audio_format", "wav"),
            username=db.get_owner(),
        )
        results = downloader.fetch_all_feeds(feeds)
        mark_config_feeds_imported(db, feeds)

        db.update_job(
            job_id,
            status="completed",
            progress=1.0,
            message=(
                f"Imported {len(feeds)} feed(s) from the config file,"
                f" {sum(results.values())} new episodes"
            ),
        )
    except Exception as e:
        logger.exception("Feed import failed")
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

        settings = _get_settings()
        transcriber = None
        try:
            _begin_transcription(settings)
            transcriber = _make_transcriber(db, settings)

            db.update_job(job_id, progress=0.2, message=f"Transcribing: {title}")
            stop_heartbeat = _start_heartbeat(db, job_id, f"Transcribing: {title}")
            try:
                success = transcriber.transcribe_episode(episode_id)
            finally:
                stop_heartbeat()
        finally:
            if transcriber is not None:
                transcriber.unload_models()
            model_gate.resume()

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


def task_index(job_id: str):
    """Add every processed episode missing from the Ask index (or embedded
    with another model) to it: the backfill after enabling Ask or changing
    the embedding model. Gives way to a daily run between episodes: it stops
    and queues a new index job for the rest behind it."""
    db = get_db()
    try:
        db.update_job(job_id, status="running", message="Indexing the library...")
        from p3.indexer import index_pending

        def on_progress(done: int, total: int, message: str):
            db.update_job(
                job_id, progress=done / total if total else 0, message=message
            )

        stats = index_pending(
            db,
            _make_embedder(_get_settings()),
            progress=on_progress,
            should_stop=lambda: job_runner.priority_waiting,
        )
        left = stats["pending"] - stats["indexed"] - stats["failed"]
        if left > 0:
            next_job = db.create_job("index")
            job_runner.enqueue(task_index, next_job)
        db.update_job(
            job_id,
            status="completed" if not stats["failed"] else "failed",
            progress=1.0,
            message=(
                f"Indexed {stats['indexed']} of {stats['pending']} episodes"
                + (f", {stats['failed']} failed" if stats["failed"] else "")
                + (f"; {left} left for the next index job" if left > 0 else "")
            ),
            error=(
                f"{stats['failed']} episodes could not be indexed (see the log)"
                if stats["failed"]
                else None
            ),
        )
    except Exception as e:
        logger.exception("Index task failed")
        db.update_job(job_id, status="failed", error=str(e))


def task_digest(job_id: str, episode_id: int):
    """Generate structured summary for an episode."""
    db = get_db()
    try:
        db.update_job(job_id, status="running", message="Generating summary...")

        cleaner = _make_cleaner(db, _get_settings())

        db.update_job(job_id, progress=0.3, message="Cleaning transcript...")
        result = cleaner.generate_summary(episode_id)

        if result:
            db.update_job(job_id, progress=0.9, message="Indexing for Ask...")
            note = _index_episode(db, episode_id, _get_settings())
            db.update_job(
                job_id,
                status="completed",
                progress=1.0,
                message=f"Summary complete; {note}",
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

        writer = _make_writer(db, user_settings(username), provider, model)

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


def task_translate(job_id: str, episode_id: int):
    """Add the Quebec French version of an episode's digest (and synopsis),
    for digests made before every digest came in both languages."""
    db = get_db()
    try:
        db.update_job(job_id, status="running", message="Translating digest...")
        cleaner = _make_cleaner(db, _get_settings())
        if cleaner.translate_digest(episode_id, synopsis=True):
            db.update_job(
                job_id, status="completed", progress=1.0, message="Digest translated"
            )
        else:
            db.update_job(job_id, status="failed", error="Translation failed")
    except Exception as e:
        logger.exception("Translate task failed")
        db.update_job(job_id, status="failed", error=str(e))


# ------------------------------------------------------------------
# Batch queueing and the full per-episode pipeline
# ------------------------------------------------------------------


def queue_step_jobs(
    db,
    episodes,
    step: str,
    run_id: Optional[str] = None,
    username: Optional[str] = None,
    priority: bool = False,
) -> list[str]:
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
    enqueue = job_runner.enqueue_priority if priority else job_runner.enqueue

    job_ids = []
    for ep in episodes:
        if not eligible(ep["status"]):
            continue
        job_id = db.create_job(
            job_type,
            episode_id=ep["id"],
            podcast_id=ep["podcast_id"],
            run_id=run_id,
            username=username,
        )
        enqueue(task_fn, job_id, ep["id"])
        job_ids.append(job_id)
    return job_ids


# Each user's daily run is a chain of steps on the serial queue. Each step
# queues its jobs, tagged with the run's id, then queues the next step behind
# them, so the next step sees the results of everything before it. Runs of
# different users share the queue; an episode two of them follow is still
# processed once (the second pipeline job finds it done).


def run_key(name: str, username: str) -> str:
    """app_state key of a per-user daily run value: ``id``, ``started_at``,
    ``finished_id`` or ``last_date``."""
    return f"daily_run_{name}:{username}"


# Runs in progress in this process. A run whose id is in neither this nor
# its ``finished_id`` key was cut short by a server restart.
active_run_ids: set[str] = set()


def queue_daily_run(db, username: str) -> str:
    """Start ``username``'s daily run: fetch every podcast they follow,
    transcribe → digest every one of their episodes not yet processed, then
    their opt-in LinkedIn drafts and digest export. The owner's run first
    imports new config-file feeds. Returns the run id."""
    from p3.downloader import new_config_feeds

    run_id = str(uuid.uuid4())
    active_run_ids.add(run_id)
    db.set_state(run_key("id", username), run_id)
    db.set_state(
        run_key("started_at", username), datetime.now().isoformat(timespec="seconds")
    )

    if username == db.get_owner():
        feeds = new_config_feeds(db, load_config().get("feeds") or [])
        if feeds:
            job_id = db.create_job("import_feeds", run_id=run_id, username=username)
            job_runner.enqueue_priority(task_import_feeds, job_id, feeds)
    job_runner.enqueue_priority(daily_queue_fetches, run_id, username)
    return run_id


def resume_daily_run(
    db, username: str, max_age: Optional[timedelta] = None
) -> Optional[str]:
    """Pick up ``username``'s daily run if a restart cut it short, so it
    still reaches its last step (the emailed digest) instead of leaving
    the report unsent. Returns the run id, or None if there was nothing
    to resume.

    The jobs the restart killed are replaced by new jobs tagged with the
    same run id (so the report counts them), fetches that never finished
    are repeated, and the chain restarts at the pipeline step, which
    queues every episode still unprocessed — a no-op for those already
    done. ``max_age`` ignores a run started longer ago than that."""
    run_id = db.get_state(run_key("id", username))
    if not run_id or run_id in active_run_ids:
        return None
    if db.get_state(run_key("finished_id", username)) == run_id:
        return None
    if max_age is not None:
        try:
            started = datetime.fromisoformat(
                db.get_state(run_key("started_at", username))
            )
        except (TypeError, ValueError):
            return None
        if datetime.now() - started > max_age:
            return None

    active_run_ids.add(run_id)
    killed = [
        j
        for j in db.get_jobs_by_run(run_id)
        if j["status"] == "failed" and j["error"] == INTERRUPTED_ERROR
    ]
    db.delete_jobs([j["id"] for j in killed])

    refetched = set()
    for job in killed:
        pid = job["podcast_id"]
        if job["job_type"] != "fetch" or not pid or pid in refetched:
            continue
        if not db.get_podcast_by_id(pid):
            continue
        refetched.add(pid)
        job_id = db.create_job(
            "fetch", podcast_id=pid, run_id=run_id, username=username
        )
        job_runner.enqueue_priority(task_fetch, job_id, pid, None, None, username)

    job_runner.enqueue_priority(daily_queue_pipelines, run_id, username)
    logger.info("Resuming interrupted daily run %s for %s", run_id, username)
    return run_id


def resume_interrupted_runs(db, max_age: Optional[timedelta] = None) -> list[str]:
    """resume_daily_run for every user. Returns the users resumed."""
    return [
        user["username"]
        for user in db.get_all_users()
        if resume_daily_run(db, user["username"], max_age)
    ]


def daily_queue_fetches(run_id: str, username: str):
    """Daily run step: a fetch job for every podcast the user follows."""
    db = get_db()
    for podcast in db.get_all_podcasts(username):
        job_id = db.create_job(
            "fetch", podcast_id=podcast["id"], run_id=run_id, username=username
        )
        job_runner.enqueue_priority(
            task_fetch, job_id, podcast["id"], None, None, username
        )
    job_runner.enqueue_priority(daily_queue_pipelines, run_id, username)


def _run_since(db, run_id: str):
    """When the run's first job was created (database clock), or None."""
    created = [j["created_at"] for j in db.get_jobs_by_run(run_id) if j["created_at"]]
    return min(created) if created else None


def daily_queue_pipelines(run_id: str, username: str):
    """Daily run step: a full pipeline job, ahead of all other work, for each
    episode this run's fetches brought into the user's library. Older
    unprocessed episodes (the backlog) are left for after the report."""
    db = get_db()
    since = _run_since(db, run_id)
    episodes = db.get_episodes_added_since(since, username) if since else []
    job_ids = queue_step_jobs(db, episodes, "pipeline", run_id, username, priority=True)
    logger.info("Daily run: queued %d new episode(s) for processing", len(job_ids))
    job_runner.enqueue_priority(daily_queue_content, run_id, username)


def daily_queue_content(run_id: str, username: str):
    """Daily run step: the user's opt-in extras. A LinkedIn draft for each
    episode this run processed (``daily_run_linkedin``) and the day's digest
    export (``daily_run_export``)."""
    db = get_db()
    settings = user_settings(username)

    if settings.get("daily_run_linkedin"):
        for job in db.get_jobs_by_run(run_id):
            if job["job_type"] != "full_pipeline" or job["status"] != "completed":
                continue
            job_id = db.create_job(
                "write_linkedin",
                episode_id=job["episode_id"],
                podcast_id=job["podcast_id"],
                run_id=run_id,
                username=username,
            )
            job_runner.enqueue_priority(
                task_write_linkedin, job_id, job["episode_id"], None, None, username
            )

    if settings.get("daily_run_export"):
        today = datetime.now()
        if db.get_summaries_by_date(today, username):
            formats = [
                f
                for f in settings.get("export_format") or []
                if f in ("markdown", "json")
            ] or ["markdown", "json"]
            job_id = db.create_job("export", run_id=run_id, username=username)
            job_runner.enqueue_priority(
                task_export, job_id, today.strftime("%Y-%m-%d"), formats, username
            )

    job_runner.enqueue_priority(daily_finish, run_id, username)


# A run that could read none of its feeds (no network at run time) is tried
# again later instead of counting as the day's run, and a report email that
# failed to send is sent again later. Both every RETRY_MINUTES, a few times.
RETRY_MINUTES = 15
MAX_RUN_ATTEMPTS = 8
MAX_EMAIL_ATTEMPTS = 8


def attempts_today(db, key: str, today: str) -> int:
    """Attempt count stored as "YYYY-MM-DD:n"; 0 for another day."""
    day, _, count = (db.get_state(key) or "").partition(":")
    return int(count) if day == today and count.isdigit() else 0


def _all_fetches_failed(db, run_id: str) -> bool:
    """True if the run fetched feeds and every one reported a problem: the
    sign of a network outage rather than one broken feed."""
    fetches = [j for j in db.get_jobs_by_run(run_id) if j["job_type"] == "fetch"]
    return bool(fetches) and all(j["error"] for j in fetches)


def daily_finish(run_id: str, username: str):
    """Daily run step: mark the run finished, then email the user the run's
    report if they asked for it.

    If no feed could be read, the run is retried in RETRY_MINUTES instead
    (see scheduler.run_if_due), up to MAX_RUN_ATTEMPTS runs a day; the last
    one reports whatever happened. A report that fails to send is sent
    again later (retry_report_email). A mail problem never affects the run;
    it is logged and kept in ``app_state`` under ``email_error``."""
    db = get_db()
    db.set_state(run_key("finished_id", username), run_id)
    active_run_ids.discard(run_id)
    now = datetime.now()
    today = now.date().isoformat()
    if (
        _all_fetches_failed(db, run_id)
        and attempts_today(db, run_key("attempts", username), today) < MAX_RUN_ATTEMPTS
    ):
        retry_at = now + timedelta(minutes=RETRY_MINUTES)
        db.set_state(run_key("retry_at", username), retry_at.isoformat("T", "seconds"))
        logger.warning(
            "Daily run for %s could not read any feed; trying again at %s",
            username,
            retry_at.strftime("%H:%M"),
        )
        return
    db.set_state(run_key("retry_at", username), "")
    logger.info("Daily run finished for %s", username)
    try:
        if not _email_run_report(db, run_id, username):
            _schedule_email_retry(db, run_id, username)
    finally:
        queue_backlog(db, username)


def _schedule_email_retry(db, run_id: str, username: str):
    """Send this run's report again in RETRY_MINUTES, or give up after
    MAX_EMAIL_ATTEMPTS."""
    now = datetime.now()
    today = now.date().isoformat()
    sent_tries = attempts_today(db, run_key("email_attempts", username), today) + 1
    if sent_tries > MAX_EMAIL_ATTEMPTS:
        db.set_state(run_key("email_retry", username), "")
        logger.error("Giving up on emailing the report of run %s", run_id)
        return
    retry_at = now + timedelta(minutes=RETRY_MINUTES)
    db.set_state(run_key("email_attempts", username), f"{today}:{sent_tries}")
    db.set_state(run_key("email_retry", username), run_id)
    db.set_state(
        run_key("email_retry_at", username), retry_at.isoformat("T", "seconds")
    )
    logger.warning(
        "Will try emailing the report again at %s", retry_at.strftime("%H:%M")
    )


def retry_report_email(username: str):
    """Send a report whose email failed earlier (queued by the scheduler)."""
    db = get_db()
    run_id = db.get_state(run_key("email_retry", username))
    if not run_id:
        return
    if _email_run_report(db, run_id, username):
        db.set_state(run_key("email_retry", username), "")
        db.set_state(run_key("email_attempts", username), "")
    else:
        _schedule_email_retry(db, run_id, username)


def queue_backlog(db, username: str) -> list[str]:
    """After the report: a normal-lane pipeline job for each of the user's
    episodes still unprocessed that has no job pending or running already."""
    busy = {j["episode_id"] for j in db.get_active_jobs(username)}
    episodes = [e for e in db.get_all_episodes(username) if e["id"] not in busy]
    job_ids = queue_step_jobs(db, episodes, "pipeline", None, username)
    if job_ids:
        logger.info("Queued %d backlog episode(s) for %s", len(job_ids), username)
    _queue_index_catch_up(db)
    return job_ids


def _queue_index_catch_up(db) -> None:
    """Queue an index job behind the backlog when episodes are missing from
    the Ask index (an embedding that failed, show notes found by a later
    fetch), unless one is already waiting."""
    from p3.embedder import DEFAULT_EMBEDDING_MODEL

    model = _get_settings().get("embedding_model") or DEFAULT_EMBEDDING_MODEL
    if not db.episodes_to_index(model):
        return
    if any(j["job_type"] == "index" for j in db.get_active_jobs()):
        return
    job_runner.enqueue(task_index, db.create_job("index"))


def _email_run_report(db, run_id: str, username: str) -> bool:
    """Email the run's report if the user asked for it. Never raises.
    Returns False only if sending was attempted and failed."""
    settings = user_settings(username)
    if not settings.get("daily_run_email"):
        return True
    try:
        from p3.mailer import build_email, collect_run_report, send_email

        user = db.get_user(username) or {}
        to = settings.get("email") or user.get("email")
        if not to:
            raise RuntimeError("no email address set for this user")
        language = settings.get("language") or "en"
        # Episodes processed outside a run are reported once: those finished
        # since the last report that went out.
        cutoff = db.now()
        reported_at = db.get_state(run_key("reported_at", username))
        since = datetime.fromisoformat(reported_at) if reported_at else None
        report = collect_run_report(db, run_id, language, username, since)
        subject, text, html = build_email(
            report, datetime.now().strftime("%Y-%m-%d"), language
        )
        send_email(settings, to, subject, text, html)
        db.set_state(run_key("reported_at", username), cutoff.isoformat())
        db.set_state(run_key("email_error", username), "")
        logger.info("Daily report emailed to %s", to)
        return True
    except Exception as e:
        logger.exception("Could not email the daily report")
        db.set_state(run_key("email_error", username), str(e))
        return False


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
            transcriber = None
            try:
                _begin_transcription(settings)
                transcriber = _make_transcriber(db, settings)
                success = transcriber.transcribe_episode(episode_id)
            finally:
                if transcriber is not None:
                    transcriber.unload_models()
                model_gate.resume()
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

        db.update_job(job_id, progress=0.9, message="Indexing for Ask...")
        note = _index_episode(db, episode_id, settings)
        db.update_job(
            job_id,
            status="completed",
            progress=1.0,
            message=f"Pipeline complete; {note}",
        )
    except Exception as e:
        logger.exception("Full pipeline task failed")
        db.update_job(job_id, status="failed", error=str(e))
