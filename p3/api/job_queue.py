"""Serial in-process job runner.

Pipeline steps — transcription above all — load ML models and are memory and
GPU heavy. Running several at once can exhaust the machine, so every queued
task runs to completion before the next one starts. A single background thread
owns all pipeline work; API handlers only enqueue.
"""

import logging
import queue
import threading
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)


class JobRunner:
    """One daemon thread draining a FIFO queue, strictly one job at a time."""

    def __init__(self) -> None:
        self._q: "queue.Queue[Optional[Tuple[Callable, tuple]]]" = queue.Queue()
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
        self._q.put(None)

    def enqueue(self, fn: Callable, *args) -> None:
        """Schedule fn(*args) to run on the worker thread after everything
        already queued has finished."""
        self._q.put((fn, args))

    @property
    def depth(self) -> int:
        """Number of jobs waiting (excludes the one currently running)."""
        return self._q.qsize()

    def _run(self) -> None:
        while True:
            item = self._q.get()
            try:
                if item is None:
                    return
                fn, args = item
                try:
                    fn(*args)
                except Exception:
                    logger.exception("Queued job raised")
            finally:
                self._q.task_done()


job_runner = JobRunner()
