"""Tests for background task helpers in p3.api.tasks."""

import time
from unittest.mock import MagicMock

from p3.api.tasks import _start_heartbeat


def test_heartbeat_ticks_with_elapsed_message():
    db = MagicMock()
    stop = _start_heartbeat(db, "job1", "Working", interval=0.05)
    time.sleep(0.17)
    stop()

    assert db.update_job.call_count >= 2
    args, kwargs = db.update_job.call_args_list[0]
    assert args == ("job1",)
    assert kwargs["message"].startswith("Working (")
    assert "elapsed" in kwargs["message"]


def test_heartbeat_stop_prevents_further_ticks():
    db = MagicMock()
    stop = _start_heartbeat(db, "job1", "Working", interval=0.05)
    time.sleep(0.12)
    stop()
    count_after_stop = db.update_job.call_count

    time.sleep(0.15)
    assert db.update_job.call_count == count_after_stop


def test_heartbeat_stop_before_any_tick_is_safe():
    db = MagicMock()
    stop = _start_heartbeat(db, "job1", "Working", interval=5.0)
    stop()
    db.update_job.assert_not_called()
