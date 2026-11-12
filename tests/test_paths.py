"""Where P3 keeps its files (p3.paths) and its secrets (p3.secrets)."""

import stat

from p3 import paths, secrets
from p3.database import P3Database
from p3.exporter import DigestExporter


class TestHome:
    def test_without_p3_home_it_is_the_current_directory(self, tmp_path, monkeypatch):
        monkeypatch.delenv("P3_HOME", raising=False)
        monkeypatch.chdir(tmp_path)
        assert paths.database().resolve() == tmp_path / "data" / "p3.duckdb"

    def test_p3_home_moves_everything(self, tmp_path, monkeypatch):
        home = tmp_path / "library"
        monkeypatch.setenv("P3_HOME", str(home))
        monkeypatch.chdir(tmp_path)

        paths.ensure()
        db = P3Database()
        DigestExporter(db)
        db.close()
        secrets.write("openai_api_key", "sk-test")

        assert (home / "data" / "p3.duckdb").exists()
        assert (home / "data" / "audio").is_dir()
        assert (home / "config" / "openai_api_key.txt").exists()
        assert paths.content_dir("blog_posts", "alice") == home / "blog_posts" / "alice"
        # Nothing was written beside it.
        assert [p.name for p in tmp_path.iterdir()] == ["library"]


class TestSecrets:
    def test_the_environment_wins_over_the_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("P3_HOME", str(tmp_path))
        monkeypatch.delenv("SMTP_PASSWORD", raising=False)
        assert secrets.read("smtp_password") is None

        secrets.write("smtp_password", "  from-file \n")
        assert secrets.read("smtp_password") == "from-file"
        monkeypatch.setenv("SMTP_PASSWORD", "from-env")
        assert secrets.read("smtp_password") == "from-env"
        # What the wizard reports is the file, which is all it can change.
        assert secrets.is_saved("smtp_password")

    def test_a_saved_secret_is_private_and_an_empty_one_removes_it(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("P3_HOME", str(tmp_path))
        secrets.write("gemini_api_key", "abc")
        path = secrets.secret_file("gemini_api_key")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        secrets.write("gemini_api_key", "")
        assert not path.exists() and not secrets.is_saved("gemini_api_key")
