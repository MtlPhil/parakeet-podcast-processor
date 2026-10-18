"""Tests for writer utilities (non-LLM paths)."""

from p3.writer import BlogWriter


class TestParseGrade:
    def test_standard_format(self):
        response = "GRADE: A-\nSCORE: 91\nFEEDBACK: Great work overall."
        writer = BlogWriter(db=None)
        result = writer._parse_grade(response)
        assert result["grade"] == "A-"
        assert result["score"] == 91.0
        assert "Great work overall" in result["feedback"]

    def test_case_insensitive(self):
        response = "grade: B+\nscore: 85\nfeedback: Decent effort."
        writer = BlogWriter(db=None)
        result = writer._parse_grade(response)
        assert result["grade"] == "B+"
        assert result["score"] == 85.0

    def test_unparseable_defaults_to_zero(self):
        response = "This blog post is mediocre. I'd give it a B."
        writer = BlogWriter(db=None)
        result = writer._parse_grade(response)
        assert result["score"] == 0.0
        assert result["grade"] == "?"

    def test_decimal_score(self):
        response = "GRADE: A\nSCORE: 92.5\nFEEDBACK: Almost perfect."
        writer = BlogWriter(db=None)
        result = writer._parse_grade(response)
        assert result["score"] == 92.5


class TestParseNumberedList:
    def test_dot_format(self):
        text = "1. First item\n2. Second item\n3. Third item"
        result = BlogWriter._parse_numbered_list(text)
        assert len(result) == 3
        assert result[0] == "First item"
        assert result[2] == "Third item"

    def test_paren_format(self):
        text = "1) First item\n2) Second item"
        result = BlogWriter._parse_numbered_list(text)
        assert len(result) == 2

    def test_post_format(self):
        text = "POST 1: First post\nPOST 2: Second post\nPOST 3: Third post"
        result = BlogWriter._parse_numbered_list(text)
        assert len(result) == 3

    def test_empty_input(self):
        result = BlogWriter._parse_numbered_list("")
        assert result == []


class TestGenerateSlug:
    def test_basic_slug(self):
        assert (
            BlogWriter._generate_slug("AI's Impact on Software")
            == "ais-impact-on-software"
        )

    def test_special_characters(self):
        assert BlogWriter._generate_slug("Hello, World! #1") == "hello-world-1"

    def test_extra_spaces(self):
        assert BlogWriter._generate_slug("  too   many  spaces  ") == "too-many-spaces"

    def test_long_titles_are_truncated(self):
        slug = BlogWriter._generate_slug("word " * 100)
        assert len(slug) <= 80
        assert not slug.endswith("-")


class TestBuildContext:
    def test_single_summary(self):
        writer = BlogWriter(db=None)
        summaries = [
            {
                "episode_title": "Episode 1",
                "podcast_title": "Podcast A",
                "full_summary": "Summary text",
                "key_topics": ["AI"],
                "themes": ["tech"],
                "quotes": ["quote1"],
                "startups": ["StartupX"],
            }
        ]
        context = writer._build_context(summaries)
        assert "Episode 1" in context
        assert "Podcast A" in context
        assert "AI" in context

    def test_multiple_summaries(self):
        writer = BlogWriter(db=None)
        summaries = [
            {
                "episode_title": f"Ep {i}",
                "podcast_title": f"Pod {i}",
                "full_summary": f"Summary {i}",
                "key_topics": [f"topic{i}"],
                "themes": [],
                "quotes": [],
                "startups": [],
            }
            for i in range(3)
        ]
        context = writer._build_context(summaries)
        assert "Source 1" in context
        assert "Source 2" in context
        assert "Source 3" in context


class TestSaveLinkedInPost:
    def test_saves_both_languages_to_file(self, tmp_path):
        writer = BlogWriter(db=None)
        result = {
            "english": "This is the English post.",
            "french_quebec": "Voici le billet en français québécois.",
            "slug": "pod-a-episode-1",
            "metadata": {
                "episode_title": "Episode 1",
                "podcast_title": "Podcast A",
                "generated_at": "2026-09-12T12:00:00",
                "model_used": "llama3.2:latest",
            },
        }
        file_path = writer.save_linkedin_post(result, output_dir=str(tmp_path))
        content = open(file_path).read()
        assert "This is the English post." in content
        assert "Voici le billet en français québécois." in content
        assert "## English" in content
        assert "Français (Québec)" in content
        assert "Episode 1" in content
        assert "Podcast A" in content


class TestToneAndLanguages:
    def test_tone_is_added_to_prompts(self):
        writer = BlogWriter(db=None, tone="  Dry humour, short sentences.  ")
        assert "Dry humour, short sentences." in writer._tone_section()
        prompt = writer._build_writing_prompt("AI", "context")
        assert "Dry humour, short sentences." in prompt
        assert BlogWriter(db=None)._tone_section() == ""

    def test_split_languages(self):
        from p3.api.content import split_languages

        linkedin = "## English\n\nHello\n\n## Français (Québec)\n\nBonjour"
        assert split_languages(linkedin) == ("Hello", "Bonjour")

        blog = (
            "# Title\n\nPost\n\n## Français (Québec)\n\n# Titre\n\nBillet\n\n"
            "---\n\n## Generation Notes\n\n- Grade"
        )
        english, french = split_languages(blog)
        assert english.startswith("# Title\n\nPost") and english.endswith("- Grade")
        assert french.startswith("# Titre\n\nBillet") and french.endswith("- Grade")

        assert split_languages("Old post") == ("Old post", None)


FRENCH_TEXT = (
    "Le marché est dans une phase où les équipes ne sont pas prêtes pour la suite."
)
ENGLISH_TEXT = (
    "The market is in a phase that the teams are not ready for, but it is fine."
)


def _fake_llm(writer, calls):
    """Replace the LLM: French for writing prompts, English for translations,
    a passing grade for grading."""

    def fake(prompt, system="", max_tokens=0):
        calls.append(prompt)
        if "AP English teacher" in system:
            return "GRADE: A\nSCORE: 95\nFEEDBACK: Fine."
        if prompt.startswith("Translate the following"):
            return ENGLISH_TEXT
        return FRENCH_TEXT

    writer._generate_with_llm = fake


class TestDetectLanguage:
    def test_detects_french_and_english(self):
        from p3.writer import _detect_language

        assert _detect_language(FRENCH_TEXT) == "fr"
        assert _detect_language(ENGLISH_TEXT) == "en"
        assert _detect_language("Titre court") is None


class TestWrongLanguageRetry:
    def test_retries_once_with_a_reminder(self):
        writer = BlogWriter(db=None, language="fr")
        replies = iter([ENGLISH_TEXT, FRENCH_TEXT])
        prompts = []
        writer._generate_with_llm = lambda prompt, **kw: (
            prompts.append(prompt) or next(replies)
        )
        assert writer._generate_in_language("Écris.", "fr") == FRENCH_TEXT
        assert len(prompts) == 2 and "IMPORTANT" in prompts[1]

    def test_correct_language_is_not_retried(self):
        writer = BlogWriter(db=None)
        prompts = []
        writer._generate_with_llm = lambda prompt, **kw: (
            prompts.append(prompt) or ENGLISH_TEXT
        )
        writer._generate_in_language("Write.", "en")
        assert len(prompts) == 1


class TestFrenchWriter:
    summary = {
        "episode_title": "Ep",
        "podcast_title": "Pod",
        "full_summary": "English summary",
        "full_summary_fr": "Résumé français",
        "key_topics": ["growth"],
        "key_topics_fr": ["croissance"],
        "themes": ["scale"],
        "quotes": ["Keep it"],
        "startups": [],
    }

    def test_context_uses_french_digest_fields(self):
        context = BlogWriter(db=None, language="fr")._build_context([self.summary])
        assert "Résumé français" in context and "croissance" in context
        assert "English summary" not in context
        english = BlogWriter(db=None)._build_context([self.summary])
        assert "English summary" in english

    def test_blog_is_written_in_french_and_translated_to_english(self):
        writer = BlogWriter(db=None, language="fr")
        calls = []
        _fake_llm(writer, calls)
        result = writer.generate_blog_post_from_digest("Mon sujet", [self.summary])
        assert result["final_post_fr"] == FRENCH_TEXT
        assert result["final_post"] == ENGLISH_TEXT
        assert result["topic"] == ENGLISH_TEXT
        assert "Quebec French" in calls[0] and "Résumé français" in calls[0]
        grading = [c for c in calls if "Blog Post to Grade" in c]
        assert "judge it as a piece of French" in grading[0]

    def test_english_writer_still_adapts_to_french(self):
        writer = BlogWriter(db=None)
        calls = []
        writer._generate_with_llm = lambda prompt, system="", max_tokens=0: (
            calls.append(prompt)
            or (
                "GRADE: A\nSCORE: 95\nFEEDBACK: Fine."
                if "AP English teacher" in system
                else (FRENCH_TEXT if prompt.startswith("Traduis") else ENGLISH_TEXT)
            )
        )
        result = writer.generate_blog_post_from_digest("My topic", [self.summary])
        assert result["final_post"] == ENGLISH_TEXT
        assert result["final_post_fr"] == FRENCH_TEXT
        assert result["topic"] == "My topic"
        assert "Quebec French" not in calls[0]

    def test_linkedin_is_written_in_french_and_translated_to_english(self):
        writer = BlogWriter(db=None, language="fr")
        _fake_llm(writer, [])
        result = writer.generate_linkedin_post(self.summary)
        assert result["french_quebec"] == FRENCH_TEXT
        assert result["english"] == ENGLISH_TEXT
