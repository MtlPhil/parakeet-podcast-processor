"""The daily run: its chain of steps, the report email and its retries,
and the backlog queued after it."""

import logging
import smtplib
import uuid
from datetime import datetime, timedelta
from typing import Optional

from p3.api.deps import get_db, load_config, user_settings
from p3.api.job_queue import job_runner
from p3.api.tasks.ask import task_index
from p3.api.tasks.common import _get_settings
from p3.api.tasks.content import task_export, task_write_linkedin
from p3.api.tasks.pipeline import queue_step_jobs, task_fetch, task_import_feeds
from p3.database import INTERRUPTED_ERROR

logger = logging.getLogger(__name__)


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
# failed to send is sent again later. Both every RETRY_MINUTES, a few times;
# a report that failed only because the mail server couldn't be reached is
# tried until it is REPORT_RETRY_HOURS old (about when the next run's report
# takes over), so it goes out once the network is back.
RETRY_MINUTES = 15
MAX_RUN_ATTEMPTS = 8
MAX_EMAIL_ATTEMPTS = 8
REPORT_RETRY_HOURS = 24


def attempts_today(db, key: str, today: str) -> int:
    """Attempt count stored as "YYYY-MM-DD:n"; 0 for another day."""
    day, _, count = (db.get_state(key) or "").partition(":")
    return int(count) if day == today and count.isdigit() else 0


def _all_fetches_failed(db, run_id: str) -> bool:
    """True if the run fetched feeds and every one reported a problem: the
    sign of a network outage rather than one broken feed.

    Single-video YouTube sources don't count: once their video is in the
    library their fetch reads nothing, so it succeeds offline too."""

    def reads_network(job) -> bool:
        podcast = db.get_podcast_by_id(job["podcast_id"]) if job["podcast_id"] else None
        return not podcast or podcast.get("source_type") != "youtube_video"

    fetches = [
        j
        for j in db.get_jobs_by_run(run_id)
        if j["job_type"] == "fetch" and reads_network(j)
    ]
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
        error = _email_run_report(db, run_id, username)
        if error:
            _schedule_email_retry(db, run_id, username, _unreachable(error))
    finally:
        queue_backlog(db, username)


def _unreachable(error: Exception) -> bool:
    """Whether a send failed because the mail server couldn't be reached (no
    network, no DNS, a timeout) rather than because it refused the message
    (a wrong password, say), which trying again won't fix."""
    return isinstance(error, OSError) and not isinstance(
        error, smtplib.SMTPResponseException
    )


def _schedule_email_retry(db, run_id: str, username: str, unreachable: bool):
    """Send this run's report again in RETRY_MINUTES. Give up after
    MAX_EMAIL_ATTEMPTS, or, when the mail server couldn't be reached, once
    the first failure is REPORT_RETRY_HOURS old."""
    now = datetime.now()
    today = now.date().isoformat()
    sent_tries = attempts_today(db, run_key("email_attempts", username), today) + 1
    since_key = run_key("email_retry_since", username)
    if db.get_state(run_key("email_retry", username)) != run_id:
        db.set_state(since_key, now.isoformat("T", "seconds"))
    since = datetime.fromisoformat(db.get_state(since_key) or now.isoformat())
    if (
        now - since >= timedelta(hours=REPORT_RETRY_HOURS)
        if unreachable
        else sent_tries > MAX_EMAIL_ATTEMPTS
    ):
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
    error = _email_run_report(db, run_id, username)
    if error:
        _schedule_email_retry(db, run_id, username, _unreachable(error))
    else:
        db.set_state(run_key("email_retry", username), "")
        db.set_state(run_key("email_attempts", username), "")


# The backlog stops retrying an episode whose pipeline failed this many
# times in a row (a broken audio file fails the same way every time). It
# can still be run by hand, and a success starts the count over.
MAX_BACKLOG_FAILURES = 3


def queue_backlog(db, username: str) -> list[str]:
    """After the report: a normal-lane pipeline job for each of the user's
    episodes still unprocessed that has no job pending or running already,
    unless it failed MAX_BACKLOG_FAILURES times in a row."""
    busy = {j["episode_id"] for j in db.get_active_jobs(username)}
    failures = db.failed_in_a_row()
    episodes = []
    for e in db.get_all_episodes(username):
        if e["id"] in busy or e["status"] == "processed":
            continue
        if failures.get(e["id"], 0) >= MAX_BACKLOG_FAILURES:
            logger.warning(
                "Not retrying %s: it failed %d times in a row",
                e["title"],
                failures[e["id"]],
            )
            continue
        episodes.append(e)
    job_ids = queue_step_jobs(db, episodes, "pipeline", None, username)
    if job_ids:
        logger.info("Queued %d backlog episode(s) for %s", len(job_ids), username)
    _queue_index_catch_up(db, username)
    return job_ids


def _queue_index_catch_up(db, username: str) -> None:
    """Queue an index job behind the backlog when episodes are missing from
    the Ask index (an embedding that failed, show notes found by a later
    fetch), unless one is already waiting. The job is the user's whose run
    queued it, so it shows in their job list."""
    from p3.embedder import DEFAULT_EMBEDDING_MODEL

    model = _get_settings().get("embedding_model") or DEFAULT_EMBEDDING_MODEL
    if not db.episodes_to_index(model):
        return
    if any(j["job_type"] == "index" for j in db.get_active_jobs()):
        return
    job_runner.enqueue(task_index, db.create_job("index", username=username), username)


def _email_run_report(db, run_id: str, username: str) -> Optional[Exception]:
    """Email the run's report if the user asked for it. Never raises.
    Returns the error if sending was attempted and failed."""
    settings = user_settings(username)
    if not settings.get("daily_run_email"):
        return None
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
        return None
    except Exception as e:
        logger.exception("Could not email the daily report")
        db.set_state(run_key("email_error", username), str(e))
        return e
