"""The desktop launcher (p3.desktop), without opening a window."""

import sys
from types import SimpleNamespace

import httpx
import pytest

from p3 import desktop
from p3.api import deps


@pytest.fixture
def desktop_env(tmp_path, monkeypatch):
    """A fresh process as far as P3 is concerned: nothing set, nothing open."""
    monkeypatch.delenv("P3_HOME", raising=False)
    monkeypatch.delenv("P3_AUTH", raising=False)
    monkeypatch.setattr(desktop, "library_home", lambda: tmp_path / "P3")
    monkeypatch.setattr(deps, "_DB_PATH", None)
    monkeypatch.setattr(deps, "_CONFIG_PATH", None)
    monkeypatch.setattr(deps, "_db", None)
    yield tmp_path / "P3"
    if deps._db is not None:
        deps._db.close()


def test_the_library_goes_to_the_users_data_folder_with_no_sign_in(desktop_env):
    import os

    desktop.configure_environment()
    assert os.environ["P3_HOME"] == str(desktop_env)
    assert os.environ["P3_AUTH"] == "single"
    assert (desktop_env / "data" / "audio").is_dir()


def test_choices_already_made_are_kept(desktop_env, tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("P3_HOME", str(tmp_path / "mine"))
    monkeypatch.setenv("P3_AUTH", "local")
    desktop.configure_environment()
    assert os.environ["P3_HOME"] == str(tmp_path / "mine")
    assert os.environ["P3_AUTH"] == "local"


def test_the_window_opens_on_a_running_server_and_closing_it_stops_it(
    desktop_env, monkeypatch
):
    seen = {}

    def create_window(title, url, **size):
        seen["title"], seen["url"] = title, url

    def start():
        # The window is "open": the app behind it answers, as the owner.
        seen["status"] = httpx.get(seen["url"] + "/api/auth/status").json()
        seen["me"] = httpx.get(seen["url"] + "/api/me").json()

    monkeypatch.setitem(
        sys.modules,
        "webview",
        SimpleNamespace(create_window=create_window, start=start),
    )
    desktop.run()

    assert seen["url"].startswith("http://127.0.0.1:")
    assert seen["status"]["mode"] == "single"
    assert (seen["me"]["username"], seen["me"]["role"]) == ("me", "admin")
    assert (desktop_env / "data" / "p3.duckdb").exists()
    with pytest.raises(httpx.ConnectError):
        httpx.get(seen["url"] + "/api/auth/status")


def test_without_pywebview_it_says_what_to_install(desktop_env, monkeypatch):
    monkeypatch.setitem(sys.modules, "webview", None)
    with pytest.raises(RuntimeError, match=r"p3\[desktop\]"):
        desktop.run()
