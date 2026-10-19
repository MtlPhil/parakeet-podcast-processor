"""Multi-user library rules: first-user takeover, subscriptions, episode
links and tombstones, scoped queries, and the downloader's linking."""

from datetime import datetime

import pytest

from p3.database import P3Database
from p3.downloader import PodcastDownloader


@pytest.fixture
def db(tmp_path):
    database = P3Database(str(tmp_path / "test.duckdb"))
    yield database
    database.close()


def _podcast_with_episodes(db, n=2, url="http://example.com/rss"):
    pid = db.add_podcast("Pod", url)
    eids = [
        db.add_episode(pid, f"Ep {i}", datetime(2026, 10, i + 1), f"{url}/{i}")
        for i in range(n)
    ]
    return pid, eids


class TestUsers:
    def test_first_user_is_admin_and_takes_over_the_library(self, db):
        pid, eids = _podcast_with_episodes(db)

        ann = db.ensure_user("ann", "Ann", "ann@example.com")
        bob = db.ensure_user("bob")

        assert ann["role"] == "admin" and bob["role"] == "user"
        assert db.get_owner() == "ann"
        assert [p["id"] for p in db.get_all_podcasts("ann")] == [pid]
        assert len(db.get_all_episodes("ann")) == 2
        assert db.get_all_podcasts("bob") == []
        assert db.get_all_episodes("bob") == []

    def test_profile_updates_from_headers(self, db):
        db.ensure_user("ann")
        assert db.ensure_user("ann", "Ann A.")["display_name"] == "Ann A."
        assert db.ensure_user("ann")["display_name"] == "Ann A."

    def test_settings_round_trip(self, db):
        db.ensure_user("ann")
        db.update_user_settings("ann", {"language": "fr", "tone": "Direct."})
        assert db.get_user("ann")["settings"] == {"language": "fr", "tone": "Direct."}

    def test_role_validation(self, db):
        db.ensure_user("ann")
        db.ensure_user("bob")
        db.set_user_role("bob", "admin")
        assert db.get_user("bob")["role"] == "admin"
        with pytest.raises(ValueError):
            db.set_user_role("bob", "root")


class TestLibraries:
    def test_hidden_episode_stays_hidden_until_relinked(self, db):
        db.ensure_user("ann")
        db.ensure_user("bob")
        pid, (e1, e2) = _podcast_with_episodes(db)
        for user in ("ann", "bob"):
            db.subscribe(user, pid)
            db.link_episode(user, e1)

        assert db.hide_episode("ann", e1) == 1  # bob still has it
        assert not db.user_has_episode("ann", e1)
        url = db.get_episode_by_id(e1)["url"]
        assert db.is_url_deleted("ann", url)
        assert db.link_episode("ann", e1) is True
        assert not db.is_url_deleted("ann", url)

    def test_deleting_the_last_copy_erases_the_episode_but_remembers_it(self, db):
        db.ensure_user("ann")
        pid, (e1, _) = _podcast_with_episodes(db)
        db.subscribe("ann", pid)
        db.link_episode("ann", e1)
        db.add_transcript_segments(
            e1, [{"start": 0, "end": 1, "text": "hi", "speaker": "", "confidence": 1}]
        )
        url = db.get_episode_by_id(e1)["url"]

        assert db.hide_episode("ann", e1) == 0

        assert db.get_episode_by_id(e1) is None
        assert db.get_transcripts_for_episode(e1) == []
        assert db.is_url_deleted("ann", url)

    def test_unsubscribe_counts_remaining_subscribers(self, db):
        pid, (e1, _) = _podcast_with_episodes(db)
        db.ensure_user("ann")  # the owner takes over the podcast
        db.ensure_user("bob")
        db.subscribe("bob", pid)
        db.link_episode("bob", e1)

        assert db.unsubscribe("bob", pid) == 1  # ann (the owner) remains
        assert db.get_all_episodes("bob") == []
        assert db.unsubscribe("ann", pid) == 0

    def test_search_and_stats_are_scoped(self, db):
        pid, (e1, e2) = _podcast_with_episodes(db)
        db.add_transcript_segments(e1, [{"start": 0, "end": 1, "text": "hello world"}])
        db.ensure_user("ann")
        db.ensure_user("bob")
        db.subscribe("bob", pid)

        assert [r["id"] for r in db.search("hello", username="ann")] == [e1]
        assert db.search("hello", username="bob") == []
        assert db.get_stats("bob")["total_podcasts"] == 1
        assert db.get_stats("bob")["total_episodes"] == 0

    def test_job_visibility(self, db):
        db.ensure_user("ann")
        db.ensure_user("bob")
        pid, (e1, _) = _podcast_with_episodes(db)  # created after ann: not hers
        mine = db.create_job("export", username="bob")
        about_pod = db.create_job("transcribe", episode_id=e1, podcast_id=pid)

        assert db.job_visible_to(mine, "bob")
        assert not db.job_visible_to(mine, "ann")
        assert not db.job_visible_to(about_pod, "bob")
        db.subscribe("bob", pid)
        assert db.job_visible_to(about_pod, "bob")
        assert {j["id"] for j in db.get_recent_jobs(None, "bob")} == {mine, about_pod}
        assert db.clear_finished_jobs("ann") == 0


class TestDownloaderLinking:
    """process_feed downloads an episode once and links it per user."""

    def _feed(self, monkeypatch, tmp_path, db, username, urls):
        downloader = PodcastDownloader(
            db=db, data_dir=str(tmp_path), max_episodes=5, username=username
        )
        entries = [
            {"title": u, "url": u, "date": None, "description": "", "guid": u}
            for u in urls
        ]
        monkeypatch.setattr(downloader, "_rss_episodes", lambda url: entries)
        downloads = []

        def fake_download(url, filename):
            downloads.append(url)
            path = tmp_path / f"{filename}.wav"
            path.write_bytes(b"audio")
            return str(path)

        monkeypatch.setattr(downloader, "download_episode", fake_download)
        return downloader, downloads

    def test_second_user_gets_existing_episodes_without_a_download(
        self, db, tmp_path, monkeypatch
    ):
        db.ensure_user("ann")
        db.ensure_user("bob")
        urls = ["http://x/1.mp3", "http://x/2.mp3"]
        ann, ann_downloads = self._feed(monkeypatch, tmp_path, db, "ann", urls)
        ann.add_feed("Pod", "http://x/rss")
        assert ann.process_feed("http://x/rss") == 2
        assert len(ann_downloads) == 2

        bob, bob_downloads = self._feed(monkeypatch, tmp_path, db, "bob", urls)
        bob.add_feed("Pod", "http://x/rss")
        assert bob.process_feed("http://x/rss") == 2
        assert bob_downloads == []
        assert len(db.get_all_episodes("bob")) == 2

    def test_deleted_episode_is_not_brought_back_unless_hand_picked(
        self, db, tmp_path, monkeypatch
    ):
        db.ensure_user("ann")
        url = "http://x/1.mp3"
        ann, _ = self._feed(monkeypatch, tmp_path, db, "ann", [url])
        ann.add_feed("Pod", "http://x/rss")
        ann.process_feed("http://x/rss")
        eid = db.get_episode_id_by_url(url)
        db.hide_episode("ann", eid)

        assert ann.process_feed("http://x/rss") == 0
        assert db.get_episode_id_by_url(url) is None  # erased, not just hidden
        assert ann.process_feed("http://x/rss", episode_guids=[url]) == 1
        assert db.user_has_episode("ann", db.get_episode_id_by_url(url))
        assert not db.is_url_deleted("ann", url)

    def test_add_feed_subscribes_the_user(self, db, tmp_path):
        db.ensure_user("ann")
        db.ensure_user("bob")
        downloader = PodcastDownloader(db=db, data_dir=str(tmp_path), username="bob")
        pid = downloader.add_feed("Pod", "http://x/rss")
        assert db.is_subscribed("bob", pid)
        assert not db.is_subscribed("ann", pid)


class TestDeletedEpisodesList:
    def test_list_records_title_and_can_be_allowed_again(self, db):
        db.ensure_user("ann")
        pid, (e1, _) = _podcast_with_episodes(db)
        db.subscribe("ann", pid)
        db.link_episode("ann", e1)
        url = db.get_episode_by_id(e1)["url"]

        db.hide_episode("ann", e1)
        [item] = db.get_deleted_episodes("ann")
        assert item["url"] == url
        assert item["title"] == "Ep 0" and item["podcast_title"] == "Pod"
        assert item["deleted_at"] is not None
        assert db.get_deleted_episodes("bob") == []

        assert db.allow_deleted_episode("ann", url) is True
        assert db.allow_deleted_episode("ann", url) is False
        assert not db.is_url_deleted("ann", url)
