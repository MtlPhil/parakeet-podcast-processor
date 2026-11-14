"""Tests for speakers: turn cleanup, word-level alignment, naming and its
checks, and where names show up (digest text, Ask passages, the transcript
endpoint).

No model is loaded: the diarizer and the LLM are stand-ins.
"""

import json
import os
from datetime import datetime

import pytest

from p3.chunker import transcript_chunks
from p3.database import P3Database
from p3.indexer import index_episode
from p3.speakers import (
    assign_speakers,
    clean_turns,
    display_names,
    name_speakers,
    parse_names,
    speaker_stats,
    speaker_text,
)
from p3.transcriber import AudioTranscriber


@pytest.fixture
def db(tmp_path):
    database = P3Database(str(tmp_path / "test.duckdb"))
    yield database
    database.close()


def _turn(start, end, speaker):
    return {"start": start, "end": end, "speaker": speaker}


def _words(start, *texts, step=0.5):
    return [
        {"start": start + i * step, "end": start + (i + 1) * step, "text": f" {t}"}
        for i, t in enumerate(texts)
    ]


class FakeLLM:
    is_configured = True

    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def chat(self, system, user, max_tokens=2000):
        self.prompts.append(user)
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply)


def _episode(db, title="Souveraineté numérique", description=None):
    podcast = db.add_podcast("Les Dérangeants", "http://example.com/feed")
    episode = db.add_episode(
        podcast, title, datetime(2026, 10, 1), "http://x/1", description=description
    )
    return episode


def _stored(episode_id, db, rows):
    """Store (start, end, speaker, text) rows as an episode's transcript."""
    db.add_transcript_segments(
        episode_id,
        [{"start": a, "end": b, "speaker": s, "text": t} for a, b, s, t in rows],
    )


class TestAlignment:
    def test_blips_inside_another_turn_go_short_turns_alone_stay(self):
        turns = [
            _turn(0, 10, "A"),
            _turn(4, 4.3, "B"),  # crosstalk inside A's turn
            _turn(10.2, 10.8, "B"),  # "OK." on its own
            _turn(11, 20, "A"),
        ]
        assert [t["speaker"] for t in clean_turns(turns)] == ["A", "B", "A"]

    def test_a_segment_is_split_where_the_speaker_changes(self):
        segment = {
            "start": 0,
            "end": 4,
            "text": "Thanks for having me Ash. Great to be here.",
            "confidence": 0.9,
            "words": _words(0, "Welcome", "back,", "Zach.")
            + _words(1.5, "Thanks", "for", "having", "me."),
        }
        turns = [_turn(0, 1.5, "HOST"), _turn(1.5, 4, "GUEST")]
        out = assign_speakers([segment], turns)
        assert [(s["speaker"], s["text"]) for s in out] == [
            ("HOST", "Welcome back, Zach."),
            ("GUEST", "Thanks for having me."),
        ]
        assert out[1]["start"] == 1.5 and out[1]["confidence"] == 0.9
        assert all("words" not in s for s in out)

    def test_a_lone_word_joins_its_neighbour(self):
        segment = {
            "start": 0,
            "end": 3,
            "text": "",
            "words": _words(0, "so", "what", "um", "do", "you", "think"),
        }
        # "um" (1.0-1.5) falls in B's blip-sized turn, which isn't a blip
        # (nothing else covers it) but is one word long.
        turns = [_turn(0, 1.0, "A"), _turn(1.0, 1.5, "B"), _turn(1.5, 3, "A")]
        out = assign_speakers([segment], turns)
        assert [(s["speaker"], s["text"]) for s in out] == [
            ("A", "so what um do you think")
        ]

    def test_a_fragment_under_half_a_second_joins_its_neighbour(self):
        # "C'" "est" fall 0.3 s before the turn edge: two words, but too short.
        words = _words(1.7, "C'", "est", step=0.15) + _words(
            2.0, "pas", "une", "course"
        )
        segment = {"start": 1.7, "end": 3.5, "text": "", "words": words}
        out = assign_speakers([segment], [_turn(0, 2.0, "A"), _turn(2.0, 9, "B")])
        assert [(s["speaker"], s["text"]) for s in out] == [
            ("B", "C' est pas une course")
        ]

    def test_without_words_the_segment_takes_the_main_speaker(self):
        segments = [{"start": 0, "end": 10, "text": "Hello."}]
        turns = [_turn(0, 3, "A"), _turn(3, 10, "B")]
        assert assign_speakers(segments, turns)[0]["speaker"] == "B"

    def test_words_in_a_gap_take_the_nearest_turn(self):
        segment = {
            "start": 5,
            "end": 6,
            "text": "",
            "words": _words(5.2, "yes", "sure"),
        }
        out = assign_speakers([segment], [_turn(0, 5, "A"), _turn(9, 12, "B")])
        assert out[0]["speaker"] == "A"
        far = {"start": 20, "end": 21, "text": "", "words": _words(20, "far", "away")}
        assert assign_speakers([far], [_turn(0, 5, "A")])[0]["speaker"] is None

    def test_no_turns_changes_nothing_but_the_words(self):
        segment = {"start": 0, "end": 1, "text": "Hi.", "words": _words(0, "Hi.")}
        assert assign_speakers([segment], []) == [
            {"start": 0, "end": 1, "text": "Hi.", "speaker": None}
        ]


class TestNaming:
    PROMPT = (
        "Show notes: Étienne Crevier reçoit Joëlle Pineau.\n"
        "SPEAKER_00: Étienne Crevier à l'animation.\nSPEAKER_01: Merci Étienne."
    )

    def test_names_must_appear_in_the_text(self):
        reply = {
            "SPEAKER_00": {"name": "Etienne Crevier", "role": "host"},
            "SPEAKER_01": {"name": "Yoshua Bengio", "role": "guest"},
            "SPEAKER_02": {"name": "Ash", "role": "pundit"},
        }
        names = parse_names(
            json.dumps(reply), ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"], self.PROMPT
        )
        assert names["SPEAKER_00"] == {"name": "Etienne Crevier", "role": "host"}
        assert names["SPEAKER_01"] == {"name": None, "role": "guest"}
        # "Ash" isn't a word of the text, even inside "dashboard".
        assert names["SPEAKER_02"] == {"name": None, "role": "other"}

    def test_a_name_the_transcript_misspells_by_a_letter_is_kept(self):
        """Seen on a real episode: Whisper wrote "Abattiello", the model
        "Abbatiello"."""
        text = "Je fais cet épisode avec Elisabeth Abattiello aujourd'hui."
        reply = {
            "SPEAKER_00": {"name": "Elisabeth Abbatiello", "role": "host"},
            "SPEAKER_01": {"name": "Elisabeth Tremblay", "role": "guest"},
        }
        names = parse_names(json.dumps(reply), ["SPEAKER_00", "SPEAKER_01"], text)
        assert names["SPEAKER_00"]["name"] == "Elisabeth Abbatiello"
        assert names["SPEAKER_01"]["name"] is None

    def test_a_name_goes_to_one_speaker_and_junk_is_ignored(self):
        reply = {
            "SPEAKER_00": {"name": "Joëlle Pineau", "role": "guest"},
            "SPEAKER_01": {"name": "joelle pineau", "role": "guest"},
        }
        names = parse_names(
            json.dumps(reply), ["SPEAKER_00", "SPEAKER_01"], self.PROMPT
        )
        assert names["SPEAKER_01"]["name"] is None
        assert parse_names("not json", ["SPEAKER_00"], self.PROMPT) == {}

    def test_name_speakers_stores_names_roles_and_talk_time(self, db):
        episode = _episode(db, description="<p>Avec Joëlle Pineau.</p>")
        _stored(
            episode,
            db,
            [
                (0, 30, "SPEAKER_03", "Le Fonds de solidarité présente."),
                (30, 40, "SPEAKER_00", "Étienne Crevier à l'animation."),
                (
                    40,
                    100,
                    "SPEAKER_01",
                    "Merci Étienne, j'ai passé huit ans chez Meta.",
                ),
            ],
        )
        llm = FakeLLM(
            {
                "SPEAKER_00": {"name": "Étienne Crevier", "role": "host"},
                "SPEAKER_01": {"name": "Joëlle Pineau", "role": "guest"},
                "SPEAKER_03": {"name": None, "role": "ad"},
            }
        )
        stored = name_speakers(db, episode, llm)
        assert db.get_episode_speakers(episode) == stored
        assert stored["SPEAKER_01"] == {
            "name": "Joëlle Pineau",
            "role": "guest",
            "seconds": 60.0,
        }
        assert "Avec Joëlle Pineau." in llm.prompts[0]  # show notes, as text
        assert display_names(stored) == {
            "SPEAKER_01": "Joëlle Pineau",
            "SPEAKER_03": "Ad",
            "SPEAKER_00": "Étienne Crevier",
        }

    def test_the_prompt_shows_the_opening_where_introductions_are(self, db):
        episode = _episode(db)
        _stored(
            episode,
            db,
            [
                (0, 5, "B", "Teaser: est-ce trop tard?"),
                (60, 70, "A", "Étienne Crevier à l'animation."),
                (70, 75, "A", "Mon invitée, Joëlle Pineau."),
                (75, 80, "B", "Merci Étienne."),
                (
                    400,
                    460,
                    "B",
                    "J'ai passé huit ans chez Meta à diriger la recherche.",
                ),
                (900, 905, "A", "Merci."),
            ],
        )
        llm = FakeLLM({})
        name_speakers(db, episode, llm)
        prompt = llm.prompts[0]
        assert (
            "[1:00] A: Étienne Crevier à l'animation. Mon invitée, Joëlle Pineau.\n"
            "[1:15] B: Merci Étienne." in prompt
        )
        assert "[6:40] J'ai passé huit ans chez Meta" in prompt
        assert "(nothing after the opening)" in prompt  # A's "Merci." is too short

    def test_a_voice_first_heard_later_gets_its_introduction(self, db):
        episode = _episode(db)
        _stored(
            episode,
            db,
            [
                (0, 10, "A", "Bienvenue, Étienne Crevier à l'animation."),
                (10, 20, "B", "Et moi, Joëlle Pineau."),
                (600, 610, "A", "On parlait de régulation tantôt."),
                (1150, 1170, "A", "Je reçois maintenant Marie Tremblay, avocate."),
                (1170, 1180, "C", "Merci de m'accueillir, Étienne."),
                (1180, 1190, "C", "La loi date de 2019."),
                (1250, 1260, "C", "Plus tard, une autre idée."),
            ],
        )
        llm = FakeLLM({"C": {"name": "Marie Tremblay", "role": "guest"}})
        stored = name_speakers(db, episode, llm)
        prompt = llm.prompts[0]
        assert (
            "C is first heard after the opening. Around its first turn:\n<<<\n"
            "[19:10] A: Je reçois maintenant Marie Tremblay, avocate.\n"
            "[19:30] C: Merci de m'accueillir, Étienne. La loi date de 2019.\n>>>"
        ) in prompt  # a minute before, its first half minute, not 10:00
        assert prompt.count("is first heard after the opening") == 1  # A, B: opening
        assert stored["C"]["name"] == "Marie Tremblay"

    def test_a_late_voice_keeps_the_lines_nearest_its_first_turn(self, db):
        episode = _episode(db)
        rows = [(0, 5, "A", "Bonjour.")]
        rows += [
            (1000 + i, 1001 + i, "A", f"phrase{i} " + "x" * 200) for i in range(59)
        ]
        rows += [(1060, 1065, "B", "Ma première phrase.")]
        _stored(episode, db, rows)
        llm = FakeLLM({})
        name_speakers(db, episode, llm)
        turn = llm.prompts[0].split("Around its first turn:\n")[1]
        assert "phrase58" in turn and "Ma première phrase." in turn
        assert "phrase0 " not in turn  # the earliest lines went over the budget

    def test_without_an_llm_speakers_are_numbered_by_talk_time(self, db):
        episode = _episode(db)
        _stored(episode, db, [(0, 5, "A", "Hi."), (5, 50, "B", "Long answer.")])
        stored = name_speakers(db, episode, None)
        assert display_names(stored) == {"B": "Speaker 1", "A": "Speaker 2"}

    def test_a_failing_llm_still_stores_the_speakers(self, db):
        episode = _episode(db)
        _stored(episode, db, [(0, 5, "A", "Hi.")])

        class Broken(FakeLLM):
            def chat(self, *a, **k):
                raise RuntimeError("model gone")

        assert name_speakers(db, episode, Broken({}))["A"]["name"] is None

    def test_no_speakers_no_rows(self, db):
        episode = _episode(db)
        _stored(episode, db, [(0, 5, None, "Hi.")])
        assert name_speakers(db, episode, FakeLLM({})) == {}
        assert speaker_stats(db.get_transcripts_for_episode(episode)) == {}


class TestStorage:
    def test_new_transcript_forgets_old_names_and_purge_erases_them(self, db):
        episode = _episode(db)
        db.replace_episode_speakers(episode, {"A": {"name": "X", "role": "host"}})
        _stored(episode, db, [(0, 5, "A", "Hi.")])
        assert db.get_episode_speakers(episode) == {}
        db.replace_episode_speakers(episode, {"A": {"name": "X", "role": "host"}})
        db.purge_episode(episode)
        assert db.get_episode_speakers(episode) == {}


class TestWhereNamesShow:
    ROWS = [
        (0, 10, "A", "Bienvenue aux Dérangeants."),
        (10, 20, "A", "Mon invitée, Joëlle Pineau."),
        (20, 40, "B", "Merci de me recevoir."),
    ]

    def test_digest_text_names_each_change_of_speaker(self, db):
        episode = _episode(db)
        _stored(episode, db, self.ROWS)
        names = {"A": "Étienne Crevier", "B": "Joëlle Pineau"}
        text = speaker_text(db.get_transcripts_for_episode(episode), names)
        assert text.splitlines() == [
            "Étienne Crevier: Bienvenue aux Dérangeants. Mon invitée, Joëlle Pineau.",
            "Joëlle Pineau: Merci de me recevoir.",
        ]

    def test_without_speakers_the_text_is_one_line_as_before(self):
        segments = [{"text": "Hi.", "speaker": None}, {"text": " Bye. "}]
        assert speaker_text(segments, {}) == "Hi. Bye."

    def _cleaner(self, db, naming_llm=None):
        from p3.cleaner import TranscriptCleaner

        return TranscriptCleaner(db=db, llm_provider="ollama", naming_llm=naming_llm)

    def test_the_naming_llm_names_speakers_once(self, db):
        episode = _episode(db)
        _stored(episode, db, self.ROWS)
        naming = FakeLLM({"B": {"name": "Joëlle Pineau", "role": "guest"}})
        cleaner = self._cleaner(db, naming)
        cleaner.llm = FakeLLM({})  # the digest's LLM: not asked
        cleaner._name_speakers(episode)
        text = cleaner._transcript(episode)
        assert "Joëlle Pineau: Merci de me recevoir." in text
        assert "Speaker 1: Bienvenue" in text
        cleaner._name_speakers(episode)
        assert len(naming.prompts) == 1  # the stored names are reused
        assert cleaner.llm.prompts == []

    def test_without_a_naming_llm_the_digest_llm_names(self, db):
        episode = _episode(db)
        _stored(episode, db, self.ROWS)
        cleaner = self._cleaner(db)
        cleaner.llm = FakeLLM({"B": {"name": "Joëlle Pineau", "role": "guest"}})
        cleaner._name_speakers(episode)
        assert len(cleaner.llm.prompts) == 1

    def test_the_digest_reads_the_named_transcript(self, db, monkeypatch):
        episode = _episode(db)
        _stored(episode, db, self.ROWS)
        naming = FakeLLM({"B": {"name": "Joëlle Pineau", "role": "guest"}})
        cleaner = self._cleaner(db, naming)
        read = []
        monkeypatch.setattr(
            cleaner,
            "_generate_structured_summary",
            lambda text, context: read.append(text) or {},
        )
        monkeypatch.setattr(cleaner, "_generate_short_summary", lambda t, c: "S")
        monkeypatch.setattr(cleaner, "translate_digest", lambda episode_id: True)
        cleaner.generate_summary(episode)
        assert "Joëlle Pineau: Merci de me recevoir." in read[0]

    def test_an_episode_without_speakers_asks_nobody(self, db):
        episode = _episode(db)
        _stored(episode, db, [(0, 10, None, "Bonjour.")])
        naming = FakeLLM({})
        self._cleaner(db, naming)._name_speakers(episode)
        assert naming.prompts == [] and db.get_episode_speakers(episode) == {}

    def test_ask_passages_carry_names_and_speakers(self, db):
        segments = [
            {"timestamp_start": a, "timestamp_end": b, "speaker": s, "text": t}
            for a, b, s, t in self.ROWS
        ]
        names = {"A": "Étienne Crevier", "B": "Joëlle Pineau"}
        (chunk,) = transcript_chunks(segments, names)
        assert chunk["text"] == (
            "Étienne Crevier: Bienvenue aux Dérangeants. Mon invitée, Joëlle "
            "Pineau.\nJoëlle Pineau: Merci de me recevoir."
        )
        assert chunk["speakers"] == "Étienne Crevier, Joëlle Pineau"
        (plain,) = transcript_chunks(segments)
        assert "speakers" not in plain and ":" not in plain["text"]

    def test_index_stores_passage_speakers(self, db):
        episode = _episode(db)
        _stored(episode, db, self.ROWS)
        db.replace_episode_speakers(
            episode,
            {
                "A": {"name": "Étienne Crevier", "role": "host", "seconds": 20},
                "B": {"name": None, "role": "guest", "seconds": 20},
            },
        )
        index_episode(db, episode, None)
        ids = [r[0] for r in db.conn.execute("SELECT id FROM chunks").fetchall()]
        chunk = next(iter(db.get_chunks(ids).values()))
        assert chunk["speakers"] == "Étienne Crevier, Speaker 1"


class TestTranscriber:
    def _transcriber(self, db, diarizer):
        return AudioTranscriber(db=db, use_parakeet=False, diarizer=diarizer)

    def test_diarization_splits_segments_after_unloading_models(self, db):
        events = []

        class FakeDiarizer:
            def diarize(self, path):
                events.append("diarize")
                return [_turn(0, 1.5, "A"), _turn(1.5, 4, "B")]

            def release(self):
                events.append("release")

        transcriber = self._transcriber(db, FakeDiarizer())
        transcriber.unload_models = lambda: events.append("unload")

        segments = [
            {
                "start": 0,
                "end": 3,
                "text": "Hi there. Hello you.",
                "words": _words(0, "Hi", "there.") + _words(1.5, "Hello", "you."),
            }
        ]
        out = transcriber._with_speakers("a.wav", segments)
        assert events == ["unload", "diarize", "release"]
        assert [(s["speaker"], s["text"]) for s in out] == [
            ("A", "Hi there."),
            ("B", "Hello you."),
        ]

    def test_failed_diarization_keeps_the_transcript(self, db):
        released = []

        class Broken:
            def diarize(self, path):
                raise RuntimeError("gated model")

            def release(self):
                released.append(True)

        transcriber = self._transcriber(db, Broken())
        transcriber.unload_models = lambda: None
        segments = [{"start": 0, "end": 1, "text": "Hi.", "words": []}]
        assert transcriber._with_speakers("a.wav", segments) == segments
        assert released == [True]

    def test_a_transcription_is_stored_with_its_speakers(self, db, tmp_path):
        class FakeDiarizer:
            def diarize(self, path):
                return [_turn(0, 1.5, "A"), _turn(1.5, 4, "B")]

            def release(self):
                pass

        audio = tmp_path / "ep.wav"
        audio.write_bytes(b"")
        podcast = db.add_podcast("Les Dérangeants", "http://example.com/feed")
        episode = db.add_episode(podcast, "Ép. 1", None, "http://x/1", str(audio))
        transcriber = self._transcriber(db, FakeDiarizer())
        transcriber.unload_models = lambda: None
        transcriber.detect_language = lambda path: "fr"
        transcriber.transcribe_with_whisper = lambda path, language: {
            "segments": [
                {
                    "start": 0,
                    "end": 3,
                    "text": "Salut toi. Bonjour vous.",
                    "words": _words(0, "Salut", "toi.")
                    + _words(1.5, "Bonjour", "vous."),
                }
            ],
            "language": "fr",
        }
        transcriber.transcribe_episode(episode)
        stored = db.get_transcripts_for_episode(episode)
        assert [(s["speaker"], s["text"]) for s in stored] == [
            ("A", "Salut toi."),
            ("B", "Bonjour vous."),
        ]
        assert db.get_episode_by_id(episode)["status"] == "transcribed"


def test_transcript_endpoint_names_speakers(tmp_path, monkeypatch):
    from p3.api import deps

    monkeypatch.setattr(deps, "_DB_PATH", str(tmp_path / "api.duckdb"))
    monkeypatch.setattr(deps, "_CONFIG_PATH", str(tmp_path / "feeds.yaml"))
    monkeypatch.setenv("P3_AUTH", "proxy")
    monkeypatch.setenv("P3_DEV_USER", "tester")
    deps.close_db()
    from fastapi.testclient import TestClient

    from p3.api.main import app

    client = TestClient(app)
    db = deps.get_db()
    episode = _episode(db)
    _stored(episode, db, TestWhereNamesShow.ROWS)
    db.replace_episode_speakers(
        episode, {"B": {"name": "Joëlle Pineau", "role": "guest", "seconds": 20}}
    )
    client.get("/api/me")  # tester, the first user, takes over the episode
    segments = client.get(f"/api/episodes/{episode}/transcript").json()
    assert [s["speaker_name"] for s in segments] == [None, None, "Joëlle Pineau"]
    deps.close_db()
    if os.path.exists(tmp_path / "api.duckdb"):
        os.remove(tmp_path / "api.duckdb")


def test_diarizer_is_made_only_when_installed_and_enabled(monkeypatch):
    from p3 import speakers
    from p3.settings import SHARED_DEFAULTS

    monkeypatch.setattr(speakers, "DIARIZATION_AVAILABLE", True)
    made = speakers.make_diarizer({})
    assert made is not None and made.model == SHARED_DEFAULTS["diarization_model"]
    assert speakers.make_diarizer({"diarization_enabled": False}) is None
    monkeypatch.setattr(speakers, "DIARIZATION_AVAILABLE", False)
    assert speakers.make_diarizer({}) is None


class TestNamingLLM:
    def test_unset_means_the_digests_own(self):
        from p3.speakers import naming_llm

        assert naming_llm({}) is None
        assert naming_llm({"speaker_naming_provider": ""}) is None

    def test_gemini_uses_the_gemini_model(self, monkeypatch):
        from p3.speakers import naming_llm

        monkeypatch.setenv("GEMINI_API_KEY", "key")
        settings = {
            "speaker_naming_provider": "gemini",
            "llm_model": "gemma4:12b",
            "gemini_model": "gemini-x",
        }
        llm = naming_llm(settings)
        assert llm is not None and (llm.provider, llm.model) == ("gemini", "gemini-x")

    def test_a_provider_without_a_key_falls_back(self, monkeypatch, tmp_path):
        from p3.speakers import naming_llm

        monkeypatch.setenv("P3_HOME", str(tmp_path))
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        assert naming_llm({"speaker_naming_provider": "gemini"}) is None

    def test_the_task_cleaner_gets_it(self, monkeypatch):
        from p3.api.tasks.common import _make_cleaner

        monkeypatch.setenv("GEMINI_API_KEY", "key")
        cleaner = _make_cleaner(None, {"speaker_naming_provider": "gemini"})
        assert cleaner.naming_llm.provider == "gemini"
        assert cleaner.llm.provider == "ollama"
