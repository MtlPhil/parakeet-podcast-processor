"""Keeps Ask's models and the transcription models out of memory together.

Transcription calls ``pause_for_transcription`` before loading its models:
from then on ``model_use`` refuses (raises ``ModelsPaused``), and the pause
waits for model calls already in flight to finish, so nothing of Ask's is
loading or running when transcription starts. ``resume`` lifts the pause
once the transcription models are unloaded. Every Ask model call (question
step, embedding, reranking, answer) runs inside ``model_use``.
"""

import re
import subprocess
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

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


def free_memory_percent() -> Optional[int]:
    """The system's free memory, as macOS's ``memory_pressure`` reports it
    (free plus reclaimable), or None when it can't be read."""
    try:
        out = subprocess.run(
            ["memory_pressure"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"free percentage: (\d+)%", out)
    return int(match.group(1)) if match else None


def memory_ok(min_free_percent: int) -> bool:
    """Whether there is room to load a local model: at least
    ``min_free_percent`` free. Unknown counts as room (other systems)."""
    free = free_memory_percent()
    return free is None or free >= min_free_percent


def total_memory_bytes() -> Optional[int]:
    try:
        out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5
        ).stdout
        return int(out.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


# Room kept free beyond a model's weights: Ask's own embedding model and
# reranker (~2.5 GB), the answer model's context cache and runner, and the
# rest of the Mac. Measured: gemma4:12b (7.7 GB) loaded beside them took free
# memory from 80% to 1% on a 16 GB Mac.
_MODEL_MARGIN_BYTES = int(5.5 * 1024**3)


def room_for_ollama_model(model: str, base_url: str, min_free_percent: int) -> bool:
    """Whether an Ollama model can be used without starving the Mac: it is
    loaded already, or free memory exceeds its size plus a margin (and the
    ``min_free_percent`` floor). Unknown sizes or memory count as room."""
    try:
        import ollama

        client = ollama.Client(host=base_url)
        if any(
            m.get("model") == model or m.get("name") == model
            for m in client.ps()["models"]
        ):
            return True
        size = next(
            (
                m["size"]
                for m in client.list()["models"]
                if m.get("model") in (model, f"{model}:latest")
            ),
            None,
        )
    except Exception:
        size = None
    free, total = free_memory_percent(), total_memory_bytes()
    if free is None:
        return True
    if free < min_free_percent:
        return False
    if size is None or total is None:
        return True
    return free / 100 * total >= size + _MODEL_MARGIN_BYTES
