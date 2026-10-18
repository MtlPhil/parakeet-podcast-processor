"""Long- and short-form content generation from podcast summaries.

Blog posts go through an iterative write/grade/revise loop in which a
separate "strict AP English teacher" persona scores each draft, an approach
popularized by Tomasz Tunguz. LinkedIn posts are a lighter single-pass path.
"""

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .database import P3Database
from .exporter import localized
from .llm import DEFAULT_OLLAMA_MODEL, DEFAULT_OLLAMA_URL, LLMClient

logger = logging.getLogger(__name__)

# Keeps generated filenames well under filesystem limits.
_MAX_SLUG_LENGTH = 80

# Heading that starts the French half of a saved post (LinkedIn and blog).
FRENCH_HEADING = "## Français (Québec)"

# Common function words that tell French from English; see _detect_language.
_FRENCH_WORDS = frozenset(
    "le la les des de du une et est que qui dans pour pas sur au aux ce cette "
    "nous vous avec sont mais".split()
)
_ENGLISH_WORDS = frozenset(
    "the and of to is that for with are this it was not but from by be".split()
)


def _detect_language(text: str) -> Optional[str]:
    """ "fr" or "en" by counting common function words, or None when the text
    is too short to tell."""
    words = re.findall(r"[a-zà-ÿ']+", text.lower())
    french = sum(w in _FRENCH_WORDS for w in words)
    english = sum(w in _ENGLISH_WORDS for w in words)
    if french + english < 3:
        return None
    return "fr" if french > english else "en"


class BlogWriter:
    def __init__(
        self,
        db: P3Database,
        llm_provider: str = "ollama",
        llm_model: str = DEFAULT_OLLAMA_MODEL,
        target_grade: float = 91.0,
        ollama_base_url: str = DEFAULT_OLLAMA_URL,
        tone: str = "",
        language: str = "en",
    ):
        """``tone`` is the author's own description of their voice, added to
        every writing prompt. ``language`` ("en" or "fr") is the language
        posts are written in; the other language is translated from it."""
        self.db = db
        self.tone = tone.strip()
        self.language = "fr" if language == "fr" else "en"
        self.llm_model = llm_model
        self.target_grade = target_grade
        self.max_iterations = 3
        # Writing jobs are infrequent, so the model is unloaded immediately
        # after each call instead of being kept warm.
        self.llm = LLMClient(
            provider=llm_provider,
            model=llm_model,
            ollama_base_url=ollama_base_url,
            keep_alive=0,
        )

    # ------------------------------------------------------------------
    # Core blog generation
    # ------------------------------------------------------------------

    def generate_blog_post_from_digest(
        self,
        topic: str,
        summaries: List[Dict[str, Any]],
        context_posts: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Generate blog post from one or more podcast summaries with iterative AP English grading.

        Args:
            topic: The main topic/angle for the blog post
            summaries: List of structured digest dicts from podcast analysis
            context_posts: Optional list of related blog posts for style matching

        Returns:
            Dict containing final blog post, grades, and iterations
        """

        context = self._build_context(summaries)

        # Generate initial blog post
        initial_prompt = self._build_writing_prompt(topic, context, context_posts)
        current_post = self._generate_in_language(initial_prompt, self.language)

        iterations = []

        for iteration in range(self.max_iterations):
            grade_result = self._grade_blog_post(current_post)
            iterations.append(
                {
                    "iteration": iteration + 1,
                    "post": current_post,
                    "grade": grade_result["grade"],
                    "score": grade_result["score"],
                    "feedback": grade_result["feedback"],
                }
            )

            if grade_result["score"] >= self.target_grade:
                break

            # Improve based on feedback
            if iteration < self.max_iterations - 1:
                improvement_prompt = self._build_improvement_prompt(
                    current_post, grade_result["feedback"]
                )
                current_post = self._generate_in_language(
                    improvement_prompt, self.language
                )

        slug = self._generate_slug(topic)

        # Use first summary for metadata, but all summaries contributed to context
        primary = summaries[0]

        # The post was written in the user's language; the other one is
        # translated from it so the two always say the same thing.
        if self.language == "fr":
            post_en = self._translate_to_english(current_post, "blog post")
            post_fr = current_post
            topic_en = self._translate_to_english(topic, "blog post title")
        else:
            post_en = current_post
            post_fr = self._adapt_to_quebec_french(current_post, "article de blogue")
            topic_en = topic
        topic_fr = self._adapt_to_quebec_french(topic, "titre d'article")

        return {
            "final_post": post_en,
            "final_post_fr": post_fr,
            "topic_fr": topic_fr,
            "final_grade": iterations[-1]["grade"],
            "final_score": iterations[-1]["score"],
            "iterations": iterations,
            "topic": topic_en,
            "slug": slug,
            "metadata": {
                "episode_title": primary.get("episode_title", ""),
                "podcast_title": primary.get("podcast_title", ""),
                "source_count": len(summaries),
                "generated_at": datetime.now().isoformat(),
                "model_used": self.llm_model,
            },
        }

    # ------------------------------------------------------------------
    # Context building
    # ------------------------------------------------------------------

    def _build_context(self, summaries: List[Dict[str, Any]]) -> str:
        """Build combined context from multiple podcast summaries."""
        sections = []
        for i, s in enumerate(summaries, 1):
            s = localized(s, self.language)
            episode_title = s.get("episode_title", "")
            podcast_title = s.get("podcast_title", "")
            summary = s.get("full_summary", "")
            key_topics = s.get("key_topics", [])
            themes = s.get("themes", [])
            quotes = s.get("quotes", [])
            companies = s.get("startups", [])

            section = (
                f"Source {i}: {episode_title} from {podcast_title}\n"
                f"Summary: {summary}\n"
                f"Key Topics: {', '.join(key_topics)}\n"
                f"Themes: {', '.join(themes)}\n"
                f"Notable Quotes: {quotes}\n"
                f"Companies Mentioned: {', '.join(companies)}"
            )
            sections.append(section)

        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # Prompts
    # ------------------------------------------------------------------

    def _build_writing_prompt(
        self, topic: str, context: str, context_posts: Optional[List[str]] = None
    ) -> str:
        """Build the initial writing prompt."""

        style_guidelines = """
Style Guidelines:
- 500 words or less (49 seconds with reader)
- No section headers (headers hurt dwell time)
- Flowing paragraphs that transition smoothly
- Limit each paragraph to at most two long sentences
- Strong hook in first few sentences
- Conclusion that ties back to opening
- Focus on actionable insights
- Include specific examples and quotes when relevant
"""

        context_section = ""
        if context_posts:
            joined = "\n".join(context_posts[:3])
            context_section = f"\nRelated Content for Style Reference:\n{joined}\n"

        return (
            "You are an expert blog writer specializing in technology and business content.\n"
            f"{style_guidelines}\n"
            f"Topic: {topic}\n\n"
            f"Source Material:\n{context}\n"
            f"{context_section}{self._tone_section()}{self._language_section()}\n"
            "Write a compelling blog post that:\n"
            "1. Opens with a strong hook that draws readers in\n"
            "2. Presents insights from the podcast content\n"
            "3. Provides actionable takeaways for business/tech readers\n"
            "4. Includes relevant quotes to support key points\n"
            "5. Concludes with a thought-provoking statement that ties back to the opening\n\n"
            "Remember: Be concise, engaging, and focused on delivering value quickly."
        )

    def _build_improvement_prompt(self, current_post: str, feedback: str) -> str:
        """Build prompt to improve blog post based on feedback."""
        return (
            "You are revising a blog post based on AP English teacher feedback.\n\n"
            f"Current Blog Post:\n{current_post}\n\n"
            f"Teacher Feedback:\n{feedback}\n\n"
            "Please rewrite the blog post incorporating the feedback while maintaining:\n"
            "- The core message and insights\n"
            "- Concise, engaging style (500 words or less)\n"
            "- Strong hook and conclusion\n"
            "- Smooth paragraph transitions\n"
            "- Actionable takeaways\n"
            f"{self._tone_section()}{self._language_section()}\n"
            "Focus especially on addressing the specific issues mentioned in the feedback."
        )

    def _tone_section(self) -> str:
        """The author's tone instructions as a prompt section, or ''."""
        if not self.tone:
            return ""
        return (
            "\nThe author's voice and tone (write as they would; this overrides "
            f"the default tone):\n{self.tone}\n"
        )

    def _language_section(self) -> str:
        """Instruction to write in Quebec French, or '' for English."""
        if self.language != "fr":
            return ""
        return (
            "\nLanguage: write the entire piece in Quebec French (français "
            "québécois, not the French of France: Quebec vocabulary, turns of "
            "phrase and register). Translate any quotes from the sources into "
            "French.\n"
        )

    def _adapt_to_quebec_french(self, text: str, kind: str) -> str:
        """Translate and adapt text (a post of type ``kind``, in French) to
        Quebec French, keeping the author's tone. Text already in Quebec
        French comes back unchanged."""
        tone = f"\nTon de l'auteur (à respecter) :\n{self.tone}\n" if self.tone else ""
        prompt = (
            f"Traduis et adapte le texte suivant ({kind}) en français québécois "
            "(pas le français de France : utilise le vocabulaire, les tournures et "
            "le registre du Québec). Garde le même sens, le même ton et une "
            f"longueur similaire. Si le texte est déjà en français québécois, "
            f"retourne-le tel quel.\n{tone}\n"
            f"Texte original :\n{text}\n\n"
            "Retourne uniquement le texte, sans commentaire."
        )
        return self._generate_in_language(
            prompt,
            "fr",
            system="Tu es un rédacteur professionnel qui écrit en français québécois.",
        ).strip()

    def _translate_to_english(self, text: str, kind: str) -> str:
        """Translate a ``kind`` of text (e.g. "blog post") from Quebec French
        to natural English, keeping the author's tone. Text already in
        English comes back unchanged."""
        tone = f"\nThe author's tone (keep it):\n{self.tone}\n" if self.tone else ""
        prompt = (
            f"Translate the following {kind} into natural English, keeping the "
            "same meaning, tone and length. If it is already in English, return "
            f"it unchanged.\n{tone}\n"
            f"Text:\n{text}\n\n"
            "Return only the text, no commentary."
        )
        return self._generate_in_language(
            prompt,
            "en",
            system="You are a professional translator who writes natural English.",
        ).strip()

    # ------------------------------------------------------------------
    # Grading (uses a distinct persona to reduce self-grading bias)
    # ------------------------------------------------------------------

    def _grade_blog_post(self, blog_post: str) -> Dict[str, Any]:
        """Grade a draft with a strict evaluator persona, distinct from the
        writer persona to reduce self-grading bias."""

        grading_prompt = (
            "Evaluate this blog post and provide:\n"
            "1. Letter grade (A+, A, A-, B+, B, B-, C+, C, C-, D+, D, F)\n"
            "2. Numerical score (0-100)\n"
            "3. Detailed feedback on each criterion\n\n"
            "Evaluation Criteria:\n"
            "- Hook/Opening (20 points): Does it grab attention immediately?\n"
            "- Argument Clarity (20 points): Is the main point clear and well-supported?\n"
            "- Evidence and Examples (20 points): Are quotes and examples used effectively?\n"
            "- Paragraph Structure (20 points): Do paragraphs flow smoothly with good transitions?\n"
            "- Conclusion Strength (20 points): Does it tie back and leave lasting impact?\n"
            "- Overall Engagement (bonus/penalty): Would readers stay engaged throughout?\n\n"
            f"Blog Post to Grade:\n{blog_post}\n\n"
            f"{self._grading_language_note()}"
            "Format your response EXACTLY as:\n"
            "GRADE: [Letter Grade]\n"
            "SCORE: [Numerical Score]\n"
            "FEEDBACK: [Detailed feedback with specific suggestions for improvement]"
        )

        response = self._generate_with_llm(
            grading_prompt,
            system="You are a strict AP English teacher and writing critic. "
            "You grade rigorously and are harder to impress than most readers. "
            "Be specific about weaknesses and provide actionable improvement suggestions.",
        )

        return self._parse_grade(response)

    def _grading_language_note(self) -> str:
        """Tells the grader a French draft is judged as French writing."""
        if self.language != "fr":
            return ""
        return (
            "The post is written in Quebec French: judge it as a piece of French "
            "writing (grammar, idiom, Quebec register). Keep the GRADE, SCORE and "
            "FEEDBACK labels in English.\n\n"
        )

    def _parse_grade(self, response: str) -> Dict[str, Any]:
        """Parse grade, score, and feedback from grader response.

        Falls back gracefully when the LLM doesn't follow the format exactly.
        """
        grade_match = re.search(r"GRADE:\s*([A-F][+-]?)", response, re.IGNORECASE)
        score_match = re.search(r"SCORE:\s*(\d+(?:\.\d+)?)", response, re.IGNORECASE)
        feedback_match = re.search(
            r"FEEDBACK:\s*(.*)", response, re.DOTALL | re.IGNORECASE
        )

        grade = grade_match.group(1).upper() if grade_match else None
        score = float(score_match.group(1)) if score_match else None
        feedback = feedback_match.group(1).strip() if feedback_match else response

        # An unparseable score counts as 0 so the revision loop keeps going.
        if score is None:
            logger.warning(
                "Could not parse score from grader response, defaulting to 0"
            )
            score = 0.0
        if grade is None:
            logger.warning("Could not parse letter grade from grader response")
            grade = "?"

        return {
            "grade": grade,
            "score": score,
            "feedback": feedback,
            "raw_response": response,
        }

    # ------------------------------------------------------------------
    # LLM interface
    # ------------------------------------------------------------------

    def _generate_with_llm(
        self,
        prompt: str,
        system: str = "You are an expert blog writer and writing instructor.",
        max_tokens: int = 4000,
    ) -> str:
        """Generate text with the configured LLM. Raises RuntimeError on failure."""
        try:
            return self.llm.chat(system, prompt, max_tokens=max_tokens)
        except Exception as e:
            raise RuntimeError(f"LLM generation failed: {e}") from e

    def _generate_in_language(self, prompt: str, language: str, **kwargs) -> str:
        """Like _generate_with_llm, but retries once if the reply comes back in
        the wrong language (small and large models alike sometimes drift)."""
        text = self._generate_with_llm(prompt, **kwargs)
        if _detect_language(text) in (language, None):
            return text
        logger.warning("Reply was not in %r, retrying once", language)
        reminder = {
            "fr": "\n\nIMPORTANT : réponds uniquement en français québécois.",
            "en": "\n\nIMPORTANT: reply in English only.",
        }[language]
        text = self._generate_with_llm(prompt + reminder, **kwargs)
        if _detect_language(text) not in (language, None):
            logger.warning("Reply is still not in %r", language)
        return text

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def save_blog_post(
        self, blog_result: Dict[str, Any], output_dir: str = "blog_posts"
    ) -> str:
        """Save generated blog post to file."""
        output_path = Path(output_dir)
        output_path.mkdir(exist_ok=True)

        date_str = datetime.now().strftime("%Y-%m-%d")
        filename = f"{date_str}-{blog_result['slug']}.md"
        file_path = output_path / filename

        source_count = blog_result["metadata"].get("source_count", 1)
        source_line = (
            f"{blog_result['metadata']['episode_title']} from {blog_result['metadata']['podcast_title']}"
            if source_count == 1
            else f"{source_count} podcast episodes"
        )

        content = f"""---
title: "{blog_result['topic']}"
date: {blog_result['metadata']['generated_at']}
source_episode: "{blog_result['metadata']['episode_title']}"
source_podcast: "{blog_result['metadata']['podcast_title']}"
source_count: {source_count}
final_grade: {blog_result['final_grade']}
final_score: {blog_result['final_score']}
model: {blog_result['metadata']['model_used']}
---

# {blog_result['topic']}

{blog_result['final_post']}

{FRENCH_HEADING}

# {blog_result.get('topic_fr') or blog_result['topic']}

{blog_result.get('final_post_fr', '')}

---

## Generation Notes

- **Final Grade**: {blog_result['final_grade']} ({blog_result['final_score']}/100)
- **Iterations**: {len(blog_result['iterations'])}
- **Source**: {source_line}
- **Generated**: {blog_result['metadata']['generated_at']}

### Grading History
"""

        for iteration in blog_result["iterations"]:
            content += f"""
**Iteration {iteration['iteration']}**: {iteration['grade']} ({iteration['score']}/100)
{iteration['feedback'][:200]}...

"""

        with open(file_path, "w") as f:
            f.write(content)

        return str(file_path)

    # ------------------------------------------------------------------
    # LinkedIn posts (direct from a single episode, no grading loop)
    # ------------------------------------------------------------------

    def generate_linkedin_post(self, summary: Dict[str, Any]) -> Dict[str, Any]:
        """Generate a LinkedIn post from one episode's summary, in English
        and Quebec French.

        A single generation with no grading loop, in the writer's language.
        The other language is translated from that draft rather than written
        independently, so the two stay consistent.
        """
        context = self._build_context([summary])
        episode_title = summary.get("episode_title", "")
        podcast_title = summary.get("podcast_title", "")

        english_prompt = (
            "Write a LinkedIn post about this podcast episode.\n\n"
            f"Source Material:\n{context}\n\n"
            "Requirements:\n"
            "- Professional tone for a business/tech LinkedIn audience\n"
            "- 150-200 words\n"
            "- Strong hook in the first line\n"
            "- Reference the episode and podcast by name\n"
            "- Share the most interesting insight or argument, not a generic teaser\n"
            "- End with a short takeaway or a question inviting engagement\n"
            "- At most 3 relevant hashtags at the end, no hashtag spam\n"
            f"{self._tone_section()}{self._language_section()}\n"
            "Return only the post text."
        )
        post = self._generate_in_language(
            english_prompt,
            self.language,
            system="You are a professional writer creating LinkedIn posts that "
            "summarize podcast insights for a business audience.",
        ).strip()

        if self.language == "fr":
            french_post = post
            english_post = self._translate_to_english(post, "LinkedIn post")
        else:
            english_post = post
            french_post = self._adapt_to_quebec_french(post, "billet LinkedIn")

        slug = self._generate_slug(f"{podcast_title}-{episode_title}")

        return {
            "english": english_post,
            "french_quebec": french_post,
            "slug": slug,
            "metadata": {
                "episode_title": episode_title,
                "podcast_title": podcast_title,
                "generated_at": datetime.now().isoformat(),
                "model_used": self.llm_model,
            },
        }

    def save_linkedin_post(
        self, result: Dict[str, Any], output_dir: str = "linkedin_posts"
    ) -> str:
        """Save a generated LinkedIn post (both languages) to file."""
        output_path = Path(output_dir)
        output_path.mkdir(exist_ok=True)

        date_str = datetime.now().strftime("%Y-%m-%d")
        filename = f"{date_str}-{result['slug']}.md"
        file_path = output_path / filename

        meta = result["metadata"]
        content = f"""---
title: "{meta['episode_title']}"
date: {meta['generated_at']}
source_episode: "{meta['episode_title']}"
source_podcast: "{meta['podcast_title']}"
model: {meta['model_used']}
---

## English

{result['english']}

{FRENCH_HEADING}

{result['french_quebec']}
"""

        with open(file_path, "w") as f:
            f.write(content)

        return str(file_path)

    # ------------------------------------------------------------------
    # Social media
    # ------------------------------------------------------------------

    def generate_social_posts(
        self, blog_result: Dict[str, Any]
    ) -> Dict[str, List[str]]:
        """Generate Twitter and LinkedIn posts from a finished blog post."""

        blog_post = blog_result["final_post"]
        topic = blog_result["topic"]

        twitter_prompt = (
            f"Generate 3 engaging Twitter posts based on this blog post about {topic}.\n\n"
            f"Blog Post:\n{blog_post}\n\n"
            "Requirements:\n"
            "- Each post under 280 characters\n"
            "- Include relevant hashtags\n"
            "- Make them engaging and actionable\n"
            "- Reference key insights or quotes when possible\n\n"
            "Format each post on its own line, numbered 1. 2. 3."
        )

        linkedin_prompt = (
            f"Generate 2 LinkedIn posts based on this blog post about {topic}.\n\n"
            f"Blog Post:\n{blog_post}\n\n"
            "Requirements:\n"
            "- Professional tone suitable for business audience\n"
            "- 100-200 words each\n"
            "- Include call-to-action\n"
            "- Reference source material appropriately\n\n"
            "Format each post on its own line, numbered 1. 2."
        )

        try:
            twitter_response = self._generate_with_llm(twitter_prompt)
            linkedin_response = self._generate_with_llm(linkedin_prompt)
        except RuntimeError as e:
            logger.error("Social post generation failed: %s", e)
            return {"twitter": [], "linkedin": [], "quotes": [], "insights": []}

        twitter_posts = self._parse_numbered_list(twitter_response)
        linkedin_posts = self._parse_numbered_list(linkedin_response)

        # Extract quotable excerpts from the blog post
        quotes = []
        insights = []
        sentences = blog_post.split(". ")
        for sentence in sentences:
            stripped = sentence.strip()
            if 50 < len(stripped) < 280:
                if any(
                    word in stripped.lower()
                    for word in ["key", "important", "crucial", "insight"]
                ):
                    insights.append(stripped + ".")
                elif '"' in stripped:
                    quotes.append(stripped)

        return {
            "twitter": twitter_posts,
            "linkedin": linkedin_posts,
            "quotes": quotes[:3],
            "insights": insights[:5],
        }

    @staticmethod
    def _parse_numbered_list(text: str) -> List[str]:
        """Parse a numbered list from LLM output.

        Handles formats like '1. ...', '1) ...', 'POST 1: ...' etc.
        """
        pattern = r"(?:^|\n)\s*(?:\d+[\.\)]\s*|POST\s*\d+\s*:\s*)"
        items = re.split(pattern, text)
        # The first element is whatever came before the first number — usually empty
        return [item.strip() for item in items if item.strip()]

    @staticmethod
    def _generate_slug(topic: str) -> str:
        """Generate URL-friendly slug from topic."""
        slug = re.sub(r"[^\w\s-]", "", topic.lower())
        slug = re.sub(r"[-\s]+", "-", slug).strip("-")
        return slug[:_MAX_SLUG_LENGTH].rstrip("-")
