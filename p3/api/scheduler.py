"""Daily automatic run of the pipeline, up to but not including content.

Once a day, at ``settings.daily_run_time`` (local time, "HH:MM"), every source
in the library is fetched and every new episode is transcribed and digested,
all through the serial job queue. The check repeats every minute, so a run
missed while the Mac was asleep or the server was down starts as soon as it
is back. The last run's date is kept in the database, so a restart never
runs the same day twice.
"""

import logging
import threading
from datetime import datetime
from typing import Optional, Tuple

from p3.api.deps import get_db, load_config
from p3.api.tasks import queue_daily_run

logger = logging.getLogger(__name__)

LAST_RUN_KEY = "daily_run_last_date"
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


def run_if_due(now: Optional[datetime] = None) -> bool:
    """Queue today's run if its time has passed and it has not run yet
    today. Returns True if a run was queued."""
    settings = load_config().get("settings", {})
    run_time = parse_run_time(settings.get("daily_run_time"))
    if run_time is None:
        return False

    now = now or datetime.now()
    if (now.hour, now.minute) < run_time:
        return False

    db = get_db()
    today = now.date().isoformat()
    if db.get_state(LAST_RUN_KEY) == today:
        return False

    db.set_state(LAST_RUN_KEY, today)
    job_ids = queue_daily_run(db)
    logger.info("Daily run queued: fetching %d source(s)", len(job_ids))
    return True


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
