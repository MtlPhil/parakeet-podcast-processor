import pytest

from p3 import mailer
from p3.database import P3Database


@pytest.fixture
def db(tmp_path):
    database = P3Database(str(tmp_path / "test.duckdb"))
    yield database
    database.close()


def _processed_episode(db, run_id, title, themes, themes_fr, status="completed"):
    podcast_id = db.add_podcast("Show", f"https://example.com/{title}.rss")
    episode_id = db.add_episode(podcast_id, title, None, f"https://e.com/{title}", "x")
    db.add_summary(
        episode_id=episode_id,
        full_summary=f"Summary of {title}",
        key_topics=["a"],
        themes=themes,
        quotes=[],
        startups=[],
    )
    db.update_summary_fields(
        episode_id, full_summary_fr=f"Résumé de {title}", themes_fr=themes_fr
    )
    job_id = db.create_job(
        "full_pipeline", episode_id=episode_id, podcast_id=podcast_id, run_id=run_id
    )
    db.update_job(job_id, status=status)


def test_report_groups_digests_by_theme_in_the_users_language(db):
    _processed_episode(db, "run1", "A", ["AI"], ["IA"])
    _processed_episode(db, "run1", "B", ["AI"], ["IA"])
    _processed_episode(db, "run1", "C", [], [])

    report = mailer.collect_run_report(db, "run1", "fr")
    subject, text, html = mailer.build_email(report, "2026-10-18", "fr")

    assert "3 nouveau(x)" in subject
    assert text.index("IA") < text.index("AUTRE")
    assert "Résumé de A" in text and "Summary of A" not in text
    assert "Erreurs" not in text


def test_report_lists_errors_even_on_completed_jobs(db):
    podcast_id = db.add_podcast("Show", "https://example.com/feed.rss")
    job_id = db.create_job("fetch", podcast_id=podcast_id, run_id="run2")
    db.update_job(job_id, status="completed", error="Could not read the feed")

    report = mailer.collect_run_report(db, "run2", "en")
    subject, text, html = mailer.build_email(report, "2026-10-18", "en")

    assert "0 new, 1 problems" in subject
    assert "Could not read the feed" in text
    assert "No new episodes today." in text


def test_send_requires_a_host():
    with pytest.raises(RuntimeError, match="smtp_host"):
        mailer.send_email({}, "a@b.c", "s", "t", "<p>t</p>")


def _backlog_episode(db, username, title):
    """An episode in ``username``'s library processed outside any run."""
    podcast_id = db.add_podcast("Video", f"https://example.com/{title}.rss")
    episode_id = db.add_episode(podcast_id, title, None, f"https://e.com/{title}", "x")
    db.link_episode(username, episode_id)
    db.add_summary(
        episode_id=episode_id,
        full_summary=f"Summary of {title}",
        key_topics=["a"],
        themes=["Health"],
        quotes=[],
        startups=[],
    )
    job_id = db.create_job(
        "full_pipeline", episode_id=episode_id, podcast_id=podcast_id, username=username
    )
    db.update_job(job_id, status="completed")


def test_report_adds_episodes_processed_outside_a_run(db):
    db.ensure_user("ann")
    db.ensure_user("bob")
    _processed_episode(db, "run1", "A", ["AI"], ["IA"])
    _backlog_episode(db, "ann", "Video")

    report = mailer.collect_run_report(db, "run1", "en", "ann")
    subject, text, html = mailer.build_email(report, "2026-10-25", "en")

    assert "(1 new, 1 more)" in subject
    assert text.index("Also processed since the last report") < text.index("- Video:")
    assert "Summary of Video" in text and "HEALTH" in text
    assert mailer.collect_run_report(db, "run1", "en", "bob")["earlier"] == []


def test_report_skips_episodes_already_reported(db):
    db.ensure_user("ann")
    _backlog_episode(db, "ann", "Video")

    report = mailer.collect_run_report(db, "run1", "en", "ann", since=db.now())
    subject, text, html = mailer.build_email(report, "2026-10-25", "en")

    assert report["earlier"] == []
    assert "(0 new)" in subject and "No new episodes today." in text
