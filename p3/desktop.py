"""P3 as a desktop app: the web app in a window of its own (``p3-app``).

The same server the browser version talks to is started inside this process,
on a free port that only this computer can reach, and a native window is
opened on it (through pywebview: WebKit on macOS, WebView2 on Windows, GTK
or Qt on Linux). Closing the window stops the server.

There is no sign-in (see ``single`` in p3.api.auth), and unless P3_HOME says
otherwise the library lives in the per-user application data folder.
"""

import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Tuple

from . import paths

_START_TIMEOUT = 30.0
WINDOW_TITLE = "P3"


def library_home() -> Path:
    """Where a desktop install keeps its library when P3_HOME is not set:
    ~/Library/Application Support/P3 on macOS, %LOCALAPPDATA%\\P3 on Windows,
    ~/.local/share/P3 (or under XDG_DATA_HOME) on Linux."""
    from platformdirs import user_data_dir

    return Path(user_data_dir("P3", appauthor=False))


def configure_environment() -> None:
    """Point P3 at the desktop library and turn sign-in off, leaving alone
    whatever the person set themselves."""
    os.environ.setdefault(paths.HOME_ENV, str(library_home()))
    os.environ.setdefault("P3_AUTH", "single")
    paths.ensure()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server() -> Tuple[Any, threading.Thread, str]:
    """Start the web server on this computer only, in a background thread,
    and wait until it answers. Returns the server (set ``should_exit`` to
    stop it), its thread and its address."""
    import uvicorn

    from .api.main import app

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, name="p3-server", daemon=True)
    thread.start()
    deadline = time.monotonic() + _START_TIMEOUT
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError(
                "P3 could not start. If it is already running (as the "
                "background service, or in another window), close that first: "
                "only one P3 can have the library open."
            )
        if time.monotonic() > deadline:
            server.should_exit = True
            raise RuntimeError(f"P3 did not start within {_START_TIMEOUT:.0f} seconds")
        time.sleep(0.05)
    return server, thread, f"http://127.0.0.1:{port}"


def run() -> None:
    """Open P3 in its own window and block until the window is closed."""
    try:
        import webview
    except ImportError as e:
        raise RuntimeError(
            'The desktop window needs pywebview: pip install "p3[desktop]"'
        ) from e

    configure_environment()
    server, thread, url = start_server()
    try:
        webview.create_window(WINDOW_TITLE, url, width=1200, height=820)
        webview.start()
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def main() -> None:
    try:
        run()
    except RuntimeError as e:
        raise SystemExit(f"P3: {e}")
