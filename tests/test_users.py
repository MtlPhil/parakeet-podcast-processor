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


class TestEpisodeGuid:
    """A feed that moves an episode to a new audio URL keeps its guid; the
    episode is recognized by it, not downloaded again."""

    def _fetch(self, monkeypatch, tmp_path, db, username, entries, picked=None):
        """Fetch "http://x/rss" listing ``entries`` ((url, guid) pairs) for
        ``username``. Returns (episodes added to their library, downloads)."""
        downloader = PodcastDownloader(
            db=db, data_dir=str(tmp_path), max_episodes=5, username=username
        )
        listing = [
            {"title": guid, "url": url, "date": None, "description": "", "guid": guid}
            for url, guid in entries
        ]
        monkeypatch.setattr(downloader, "_rss_episodes", lambda url: listing)
        downloads = []

        def fake_download(url, filename):
            downloads.append(url)
            path = tmp_path / f"{filename}.wav"
            path.write_bytes(b"audio")
            return str(path)

        monkeypatch.setattr(downloader, "download_episode", fake_download)
        downloader.add_feed("Pod", "http://x/rss")
        return downloader.process_feed("http://x/rss", picked), downloads

    def test_moved_episode_is_not_downloaded_again(self, db, tmp_path, monkeypatch):
        db.ensure_user("ann")
        db.ensure_user("bob")
        old = [("http://x/1.mp3", "g1")]
        new = [("http://track.example/x/1.mp3", "g1")]
        assert self._fetch(monkeypatch, tmp_path, db, "ann", old) == (1, [old[0][0]])
        eid = db.get_episode_id_by_url(old[0][0])

        assert self._fetch(monkeypatch, tmp_path, db, "ann", new) == (0, [])
        # Someone else following it later gets the stored episode, linked.
        assert self._fetch(monkeypatch, tmp_path, db, "bob", new) == (1, [])
        assert [e["id"] for e in db.get_all_episodes("bob")] == [eid]
        assert db.get_episode_id_by_url(new[0][0]) is None

    def test_episode_stored_before_guids_gets_its_guid(self, db, tmp_path, monkeypatch):
        db.ensure_user("ann")
        pid = db.add_podcast("Pod", "http://x/rss")
        eid = db.add_episode(pid, "Old", None, "http://x/1.mp3")
        db.link_episode("ann", eid)

        self._fetch(monkeypatch, tmp_path, db, "ann", [("http://x/1.mp3", "g1")])
        assert db.get_episode_by_id(eid)["guid"] == "g1"
        moved = [("http://track.example/x/1.mp3", "g1")]
        assert self._fetch(monkeypatch, tmp_path, db, "ann", moved) == (0, [])

    def test_deleted_episode_stays_deleted_under_a_new_url(
        self, db, tmp_path, monkeypatch
    ):
        db.ensure_user("ann")
        old = [("http://x/1.mp3", "g1")]
        new = [("http://track.example/x/1.mp3", "g1")]
        self._fetch(monkeypatch, tmp_path, db, "ann", old)
        db.hide_episode("ann", db.get_episode_id_by_url(old[0][0]))
        assert db.get_all_episodes() == []  # erased: nobody else had it

        assert self._fetch(monkeypatch, tmp_path, db, "ann", new) == (0, [])
        # Hand-picking it brings it back, from its new URL.
        added = self._fetch(monkeypatch, tmp_path, db, "ann", new, picked=["g1"])
        assert added == (1, [new[0][0]])
        assert db.get_deleted_episodes("ann") == []

    def test_a_guid_listed_twice_matches_nothing(self, db, tmp_path, monkeypatch):
        db.ensure_user("ann")
        entries = [("http://x/1.mp3", "dup"), ("http://x/2.mp3", "dup")]
        added, downloads = self._fetch(monkeypatch, tmp_path, db, "ann", entries)
        assert added == 2 and len(downloads) == 2


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


class TestDeleteUser:
    def test_erases_only_what_nobody_else_has(self, db, tmp_path):
        db.ensure_user("ann")
        db.ensure_user("bob")
        shared_pid, (shared_ep, bob_ep) = _podcast_with_episodes(
            db, url="http://example.com/shared"
        )
        own_pid, (own_ep, _) = _podcast_with_episodes(db, url="http://example.com/own")
        db.subscribe("ann", shared_pid)
        db.subscribe("bob", shared_pid)
        db.subscribe("bob", own_pid)
        db.link_episode("ann", shared_ep)
        for episode_id in (shared_ep, bob_ep, own_ep):
            db.link_episode("bob", episode_id)
        db.add_transcript_segments(
            own_ep,
            [{"start": 0, "end": 1, "text": "Hi", "speaker": "", "confidence": 1}],
        )
        db.add_summary(own_ep, [], [], [], [], "Summary")
        audio = tmp_path / "own.wav"
        audio.write_bytes(b"x")
        db.conn.execute(
            "UPDATE episodes SET file_path = ? WHERE id = ?", (str(audio), own_ep)
        )

        db.delete_user("bob")

        assert db.get_user("bob") is None
        assert db.get_podcast_by_id(own_pid) is None
        assert db.get_episode_by_id(own_ep) is None
        assert db.get_transcripts_for_episode(own_ep) == []
        assert not audio.exists()
        assert db.get_episode_by_id(bob_ep) is None
        assert [p["id"] for p in db.get_all_podcasts("ann")] == [shared_pid]
        assert [e["id"] for e in db.get_all_episodes("ann")] == [shared_ep]

    def test_erases_their_records_and_no_one_elses(self, db):
        db.ensure_user("ann")
        # "_" in a username must not act as a wildcard and catch "aXb".
        for username in ("a_b", "aXb"):
            db.ensure_user(username)
            db.create_session(f"hash-{username}", username, datetime(2099, 1, 1))
            db.set_state(f"daily_run_id:{username}", "run")
            db.set_state(f"setup_done:{username}", "1")
        db.set_state("setup_done", "1")
        pid, (eid, _) = _podcast_with_episodes(db)
        db.subscribe("a_b", pid)
        db.subscribe("aXb", pid)
        db.link_episode("a_b", eid)
        db.link_episode("aXb", eid)
        db.hide_episode("a_b", eid)
        their_job = db.create_job("fetch", podcast_id=pid, username="a_b")
        other_job = db.create_job("fetch", podcast_id=pid, username="aXb")

        db.delete_user("a_b")

        assert db.get_session_username("hash-a_b", datetime.now()) is None
        assert db.get_session_username("hash-aXb", datetime.now()) == "aXb"
        assert db.get_state("daily_run_id:a_b") is None
        assert db.get_state("setup_done:a_b") is None
        assert db.get_state("daily_run_id:aXb") == "run"
        assert db.get_state("setup_done:aXb") == "1"
        assert db.get_state("setup_done") == "1"
        assert db.get_deleted_episodes("a_b") == []
        assert db.get_job(their_job) is None
        assert db.get_job(other_job) is not None
