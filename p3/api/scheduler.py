"""Each user's daily automatic run of the pipeline.

Once a day, at the user's ``daily_run_time`` (local time, "HH:MM"), every
podcast they follow is fetched and every new episode of theirs transcribed
and digested, all through the serial job queue (see
``tasks.queue_daily_run``). The check repeats every minute, so a run missed
while the Mac was asleep or the server was down starts as soon as it is
back. Each user's last run date is kept in the database, so a restart never
runs the same day twice.
"""

import logging
import threading
from datetime import datetime
from typing import Optional, Tuple

from p3.api.deps import get_db, user_settings
from p3.api.job_queue import job_runner
from p3.api.tasks import (
    active_run_ids,
    attempts_today,
    queue_daily_run,
    retry_report_email,
    run_key,
)

logger = logging.getLogger(__name__)

_CHECK_INTERVAL = 60.0


def parse_run_time(value) -> Optional[Tuple[int, int]]:
    """Parse "HH:MM" into (hour, minute). None if unset or invalid."""
    if value is None or value == "":
        return None
    # YAML reads an unquoted 6:30 as the base-60 integer 390.
    if isinstance(value, int):
        value = f"{value // 60}:{value % 60:02d}"
    try:
        parsed = datetime.strptime(str(value).strip(), "%H:%M")
    except ValueError:
        logger.warning("Ignoring invalid daily_run_time %r (expected HH:MM)", value)
        return None
    return parsed.hour, parsed.minute


def run_if_due(now: Optional[datetime] = None) -> list[str]:
    """Queue today's run for every user whose run time has passed and who
    has not had one yet today. Returns the users whose run was queued."""
    now = now or datetime.now()
    today = now.date().isoformat()
    db = get_db()
    started = []
    for user in db.get_all_users():
        username = user["username"]
        _retry_email_if_due(db, username, now)
        run_time = parse_run_time(user_settings(username).get("daily_run_time"))
        if run_time is None or (now.hour, now.minute) < run_time:
            continue
        if db.get_state(run_key("last_date", username)) == today:
            # Already run today, unless it read no feed and is due a retry
            # (tasks.daily_finish).
            if not _due(db.get_state(run_key("retry_at", username)), now):
                continue
            if db.get_state(run_key("id", username)) in active_run_ids:
                continue
            attempt = attempts_today(db, run_key("attempts", username), today) + 1
            db.set_state(run_key("retry_at", username), "")
            db.set_state(run_key("attempts", username), f"{today}:{attempt}")
            queue_daily_run(db, username)
            logger.info("Daily run retry %d queued for %s", attempt, username)
            started.append(username)
            continue
        db.set_state(run_key("last_date", username), today)
        db.set_state(run_key("attempts", username), f"{today}:1")
        db.set_state(run_key("retry_at", username), "")
        queue_daily_run(db, username)
        logger.info("Daily run queued for %s", username)
        started.append(username)
    return started


def _due(when: Optional[str], now: datetime) -> bool:
    """True if ``when`` (ISO datetime, possibly empty) has passed."""
    if not when:
        return False
    try:
        return datetime.fromisoformat(when) <= now
    except ValueError:
        return False


def _retry_email_if_due(db, username: str, now: datetime):
    """Queue a failed report email's next try once its time has come."""
    if not db.get_state(run_key("email_retry", username)):
        return
    if not _due(db.get_state(run_key("email_retry_at", username)), now):
        return
    db.set_state(run_key("email_retry_at", username), "")
    job_runner.enqueue_priority(retry_report_email, username)


class DailyScheduler:
    """Background thread that calls run_if_due() on start and every minute."""

    def __init__(self, interval: float = _CHECK_INTERVAL) -> None:
        self._interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="p3-daily-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while True:
            try:
                run_if_due()
            except Exception:
                logger.exception("Daily run check failed")
            # The Ask reranker stays loaded between questions; free it once
            # nobody has asked anything for a while.
            from p3.embedder import _reranker

            if _reranker is not None:
                _reranker.release(idle_only=True)
            if self._stop.wait(self._interval):
                return


daily_scheduler = DailyScheduler()
