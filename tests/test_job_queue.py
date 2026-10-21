"""Tests for the serial job runner."""

import threading
import time

from p3.api.job_queue import JobRunner


def test_runs_jobs_serially():
    """No two queued jobs overlap, and they run in FIFO order."""
    runner = JobRunner()
    runner.start()

    active = []
    max_concurrent = 0
    order = []
    lock = threading.Lock()
    done = threading.Event()

    def job(n):
        nonlocal max_concurrent
        with lock:
            active.append(n)
            max_concurrent = max(max_concurrent, len(active))
        time.sleep(0.02)
        with lock:
            active.remove(n)
            order.append(n)
        if n == 4:
            done.set()

    for i in range(5):
        runner.enqueue(job, i)

    assert done.wait(timeout=5)
    runner.stop()

    assert max_concurrent == 1
    assert order == [0, 1, 2, 3, 4]


def test_exception_does_not_kill_worker():
    """A job that raises is logged and the next job still runs."""
    runner = JobRunner()
    runner.start()
    ran = threading.Event()

    runner.enqueue(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    runner.enqueue(ran.set)

    assert ran.wait(timeout=5)
    runner.stop()


def test_start_is_idempotent():
    runner = JobRunner()
    runner.start()
    first = runner._thread
    runner.start()
    assert runner._thread is first
    runner.stop()


def test_priority_lane_runs_before_queued_normal_work():
    """Daily-run work jumps ahead of normal work waiting in the queue, but
    never interrupts the task already running."""
    runner = JobRunner()
    gate = threading.Event()
    started = threading.Event()
    done = threading.Event()
    order = []

    runner.start()
    runner.enqueue(lambda: (started.set(), gate.wait(5), order.append("running")))
    assert started.wait(timeout=5)
    runner.enqueue(order.append, "normal-1")
    runner.enqueue(order.append, "normal-2")
    runner.enqueue_priority(order.append, "daily-1")
    runner.enqueue_priority(order.append, "daily-2")
    runner.enqueue(done.set)
    gate.set()

    assert done.wait(timeout=5)
    runner.stop()
    assert order == ["running", "daily-1", "daily-2", "normal-1", "normal-2"]
