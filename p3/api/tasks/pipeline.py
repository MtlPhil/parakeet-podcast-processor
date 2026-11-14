"""Pipeline jobs: adding sources and fetching episodes, transcribing,
digesting, translating and refreshing digests, and the full
per-episode pipeline."""

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from p3 import model_gate
from p3.api.deps import get_db, user_settings
from p3.api.job_queue import job_runner
from p3.api.tasks.common import _get_settings, _index_episode, _make_cleaner
from p3.llm import release_ollama_models
from p3.settings import setting

logger = logging.getLogger(__name__)


def _make_transcriber(db, settings: dict):
    from p3.speakers import make_diarizer
    from p3.transcriber import (
        DEFAULT_PARAKEET_MODEL,
        DEFAULT_WHISPER_MODEL,
        AudioTranscriber,
    )

    return AudioTranscriber(
        db=db,
        whisper_model=setting(settings, "whisper_model"),
        use_parakeet=setting(settings, "parakeet_enabled"),
        parakeet_model=setting(settings, "parakeet_model"),
        parakeet_languages=setting(settings, "parakeet_languages"),
        diarizer=make_diarizer(settings),
    )


def _release_ollama(settings: dict) -> None:
    """Free Ollama's memory before transcription models load, unless an admin
    turned it off."""
    if setting(settings, "release_ollama_before_transcription"):
        release_ollama_models(setting(settings, "ollama_base_url"))


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
        max_eps = max_episodes or setting(settings, "max_episodes_per_feed")

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
            audio_format=setting(settings, "audio_format"),
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
            audio_format=setting(settings, "audio_format"),
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
            max_episodes=setting(settings, "max_episodes_per_feed"),
            audio_format=setting(settings, "audio_format"),
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


def _transcribe(db, job_id: str, episode: dict, settings: dict):
    """Transcribe ``episode`` as part of job ``job_id``, whose message shows
    the time elapsed meanwhile. Ask stops loading models meanwhile (see
    _begin_transcription). The models are unloaded afterwards, whether or
    not it worked. Raises TranscriptionError if it didn't."""
    label = f"Transcribing: {episode['title']}"
    db.update_job(job_id, message=label)
    transcriber = None
    stop_heartbeat = _start_heartbeat(db, job_id, label)
    try:
        _begin_transcription(settings)
        transcriber = _make_transcriber(db, settings)
        transcriber.transcribe_episode(episode["id"])
    finally:
        stop_heartbeat()
        if transcriber is not None:
            transcriber.unload_models()
        model_gate.resume()


def task_transcribe(job_id: str, episode_id: int):
    """Transcribe a single episode."""
    task_full_pipeline(job_id, episode_id, digest=False)


# ------------------------------------------------------------------
# Digest (summarize) an episode
# ------------------------------------------------------------------


def task_digest(job_id: str, episode_id: int):
    """Generate structured summary for an episode."""
    db = get_db()
    try:
        db.update_job(job_id, status="running", message="Generating summary...")
        settings = _get_settings()
        _make_cleaner(db, settings).generate_summary(episode_id)
        db.update_job(job_id, progress=0.9, message="Indexing for Ask...")
        note = _index_episode(db, episode_id, settings)
        db.update_job(
            job_id,
            status="completed",
            progress=1.0,
            message=f"Summary complete; {note}",
        )
    except Exception as e:
        logger.exception("Digest task failed")
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


# Episodes whose digest task_refresh_digests has redone (JSON list of ids),
# so a refresh stopped by its deadline continues where it left off.
REFRESHED_KEY = "digests_refreshed_with_notes"


def _refresh_deadline(stop_at: Optional[str]) -> Optional[datetime]:
    """The next local wall-clock time ``stop_at`` ("HH:MM"), as a naive local
    datetime: compared with datetime.now(), it follows the system clock
    through a daylight-saving change."""
    if not stop_at:
        return None
    hour, minute = (int(part) for part in stop_at.split(":"))
    now = datetime.now()
    deadline = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return deadline if deadline > now else deadline + timedelta(days=1)


def task_refresh_digests(job_id: str, stop_at: Optional[str] = None):
    """Redo every digest with its show notes (see cleaner._episode_context),
    and its synopsis where there is one, keeping each digest's date. First
    finds missing show notes in the feeds and, from the transcript, the
    language of episodes transcribed before it was detected.

    Newest digests first; ones already refreshed are skipped. With
    ``stop_at`` ("HH:MM", local), no episode starts that could still be
    running then, judging by the longest one so far; queue it again to
    continue. Runs as a "refresh" job, which no daily report lists."""
    import json

    from p3.downloader import PodcastDownloader
    from p3.writer import _detect_language

    db = get_db()
    deadline = _refresh_deadline(stop_at)
    try:
        db.update_job(job_id, status="running", message="Finding show notes...")
        settings = _get_settings()
        downloader = PodcastDownloader(
            db=db, audio_format=setting(settings, "audio_format")
        )
        notes = downloader.fill_missing_show_notes()
        logger.info("Found show notes for %d episode(s)", notes)

        for row in db.get_episodes_by_status("processed"):
            if row.get("language"):
                continue
            text = " ".join(
                seg["text"] for seg in db.get_transcripts_for_episode(row["id"])
            )
            language = _detect_language(text[:20000])
            if language:
                db.set_episode_language(row["id"], language)

        done = set(json.loads(db.get_state(REFRESHED_KEY) or "[]"))
        todo = [
            s["episode_id"]
            for s in sorted(
                db.get_all_summaries(),
                key=lambda s: str(s.get("digest_date") or ""),
                reverse=True,
            )
            if s["episode_id"] not in done
        ]
        cleaner = _make_cleaner(db, settings)
        refreshed, failed, longest = 0, [], timedelta(minutes=15)
        for i, episode_id in enumerate(todo):
            if deadline and datetime.now() + longest > deadline:
                break
            db.update_job(
                job_id,
                progress=i / len(todo),
                message=f"Refreshing digest {i + 1} of {len(todo)}...",
            )
            started = datetime.now()
            try:
                cleaner.refresh_digest(episode_id)
                _index_episode(db, episode_id, settings)
            except Exception as e:
                logger.warning("Refreshing episode %d failed: %s", episode_id, e)
                failed.append(f"episode {episode_id}: {e}")
                continue
            longest = max(longest, datetime.now() - started)
            done.add(episode_id)
            db.set_state(REFRESHED_KEY, json.dumps(sorted(done)))
            refreshed += 1

        left = len(todo) - refreshed - len(failed)
        message = (
            f"Show notes found for {notes} episode(s); "
            f"{refreshed} digest(s) refreshed, {len(failed)} failed, {left} left"
        )
        db.update_job(
            job_id,
            status="completed",
            progress=1.0,
            message=message,
            error="; ".join(failed) or None,
        )
    except Exception as e:
        logger.exception("Refresh task failed")
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


def task_full_pipeline(job_id: str, episode_id: int, digest: bool = True):
    """Run the steps an episode still needs: transcribe, then digest
    (transcribe only, with ``digest`` off). A step already done is skipped,
    so the job is safe to repeat."""
    db = get_db()
    try:
        db.update_job(job_id, status="running", message="Starting...")
        episode = db.get_episode_by_id(episode_id)
        if not episode:
            raise RuntimeError(f"Episode {episode_id} not found")
        settings = _get_settings()

        if episode["status"] == "downloaded":
            db.update_job(job_id, progress=0.1)
            _transcribe(db, job_id, episode, settings)
        if digest and episode["status"] != "processed":
            db.update_job(job_id, progress=0.5, message="Generating summary...")
            _make_cleaner(db, settings).generate_summary(episode_id)
        if digest:
            db.update_job(job_id, progress=0.9, message="Indexing for Ask...")
            note = _index_episode(db, episode_id, settings)
            message = f"Pipeline complete; {note}"
        else:
            message = f"Transcribed: {episode['title']}"
        db.update_job(job_id, status="completed", progress=1.0, message=message)
    except Exception as e:
        logger.exception("Pipeline task failed")
        db.update_job(job_id, status="failed", error=str(e))
