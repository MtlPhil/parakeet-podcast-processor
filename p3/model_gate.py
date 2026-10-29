"""Keeps Ask's models and the transcription models out of memory together.

Transcription calls ``pause_for_transcription`` before loading its models:
from then on ``model_use`` refuses (raises ``ModelsPaused``), and the pause
waits for model calls already in flight to finish, so nothing of Ask's is
loading or running when transcription starts. ``resume`` lifts the pause
once the transcription models are unloaded. Every Ask model call (question
step, embedding, reranking, answer) runs inside ``model_use``.
"""

import threading
from contextlib import contextmanager
from typing import Iterator

# Set while transcription models are (or are about to be) loaded.
transcribing = threading.Event()

_cond = threading.Condition()
_active = 0
# How long transcription waits for in-flight Ask model calls (an answer
# being written can take a minute with a local model).
_DRAIN_SECONDS = 180.0


class ModelsPaused(RuntimeError):
    """A model call was refused because transcription holds the memory."""


@contextmanager
def model_use() -> Iterator[None]:
    global _active
    with _cond:
        if transcribing.is_set():
            raise ModelsPaused("models are paused while an episode is transcribed")
        _active += 1
    try:
        yield
    finally:
        with _cond:
            _active -= 1
            _cond.notify_all()


def models_allowed() -> bool:
    return not transcribing.is_set()


def pause_for_transcription(timeout: float = _DRAIN_SECONDS) -> bool:
    """Refuse new model calls and wait for those in flight. False if some
    were still running after ``timeout`` (the caller goes ahead anyway: the
    Ollama release that follows refuses to leave a model loaded)."""
    transcribing.set()
    with _cond:
        return _cond.wait_for(lambda: _active == 0, timeout)


def resume() -> None:
    transcribing.clear()
