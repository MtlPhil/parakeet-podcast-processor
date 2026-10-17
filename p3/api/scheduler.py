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
from p3.api.tasks import queue_daily_run, run_key

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
        run_time = parse_run_time(user_settings(username).get("daily_run_time"))
        if run_time is None or (now.hour, now.minute) < run_time:
            continue
        if db.get_state(run_key("last_date", username)) == today:
            continue
        db.set_state(run_key("last_date", username), today)
        queue_daily_run(db, username)
        logger.info("Daily run queued for %s", username)
        started.append(username)
    return started


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
            if self._stop.wait(self._interval):
                return


daily_scheduler = DailyScheduler()
