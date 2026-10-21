"""Serial in-process job runner.

Pipeline steps — transcription above all — load ML models and are memory and
GPU heavy. Running several at once can exhaust the machine, so every queued
task runs to completion before the next one starts. A single background thread
owns all pipeline work; API handlers only enqueue.

Two lanes: the daily run's work goes in the priority lane and everything
else (backlog, work started from the web app) in the normal lane. The worker
always takes the next priority task first, so normal work waits on hold while
a daily run is in progress. A task already running is never interrupted; the
daily run starts as soon as it finishes.
"""

import logging
import threading
from collections import deque
from typing import Callable, Deque, Optional, Tuple

logger = logging.getLogger(__name__)

_Item = Optional[Tuple[Callable, tuple]]


class JobRunner:
    """One daemon thread draining two FIFO lanes, strictly one job at a time,
    priority lane first."""

    def __init__(self) -> None:
        self._priority: Deque[_Item] = deque()
        self._normal: Deque[_Item] = deque()
        self._cond = threading.Condition()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, name="p3-job-runner", daemon=True
            )
            self._thread.start()
            logger.info("Job runner started")

    def stop(self) -> None:
        with self._cond:
            self._priority.appendleft(None)
            self._cond.notify()

    def enqueue(self, fn: Callable, *args) -> None:
        """Schedule fn(*args) to run on the worker thread after everything
        already queued has finished."""
        with self._cond:
            self._normal.append((fn, args))
            self._cond.notify()

    def enqueue_priority(self, fn: Callable, *args) -> None:
        """Schedule fn(*args) ahead of all normal work (after the task
        running now and earlier priority tasks). Used by the daily run."""
        with self._cond:
            self._priority.append((fn, args))
            self._cond.notify()

    @property
    def depth(self) -> int:
        """Number of jobs waiting (excludes the one currently running)."""
        with self._cond:
            return len(self._priority) + len(self._normal)

    def _next(self) -> _Item:
        with self._cond:
            while not self._priority and not self._normal:
                self._cond.wait()
            return (self._priority or self._normal).popleft()

    def _run(self) -> None:
        while True:
            item = self._next()
            if item is None:
                return
            fn, args = item
            try:
                fn(*args)
            except Exception:
                logger.exception("Queued job raised")


job_runner = JobRunner()
