"""Blog post generation with AP English teacher grading system.

Inspired by Tomasz Tunguz's innovative approach to AI-assisted writing
with iterative grading and improvement loops.
"""

import json
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Any
from pathlib import Path

from .database import P3Database

logger = logging.getLogger(__name__)

# Optional Ollama support for blog generation
try:
    import ollama
    OLLAMA_AVAILABLE = True
except ImportError:
    OLLAMA_AVAILABLE = False


class BlogWriter:
    def __init__(self, db: P3Database, llm_provider: str = "ollama",
                 llm_model: str = "llama3.2:latest", target_grade: float = 91.0):
        self.db = db
        self.llm_provider = llm_provider.lower()
        self.llm_model = llm_model
        self.target_grade = target_grade
        self.max_iterations = 3

    # ------------------------------------------------------------------
    # Core blog generation
    # ------------------------------------------------------------------

    def generate_blog_post_from_digest(self, topic: str,
                                       summaries: List[Dict[str, Any]],
                                       context_posts: List[str] = None) -> Dict[str, Any]:
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
        current_post = self._generate_with_llm(initial_prompt)

        iterations = []

        # Iterative grading and improvement (inspired by Tunguz's approach)
        for iteration in range(self.max_iterations):
            grade_result = self._grade_blog_post(current_post)
            iterations.append({
                'iteration': iteration + 1,
                'post': current_post,
                'grade': grade_result['grade'],
                'score': grade_result['score'],
                'feedback': grade_result['feedback']
            })

            if grade_result['score'] >= self.target_grade:
                break

            # Improve based on feedback
            if iteration < self.max_iterations - 1:
                improvement_prompt = self._build_improvement_prompt(
                    current_post, grade_result['feedback']
                )
                current_post = self._generate_with_llm(improvement_prompt)

        slug = self._generate_slug(topic)

        # Use first summary for metadata, but all summaries contributed to context
        primary = summaries[0]

        return {
            'final_post': current_post,
            'final_grade': iterations[-1]['grade'],
            'final_score': iterations[-1]['score'],
            'iterations': iterations,
            'topic': topic,
            'slug': slug,
            'metadata': {
                'episode_title': primary.get('episode_title', ''),
                'podcast_title': primary.get('podcast_title', ''),
                'source_count': len(summaries),
                'generated_at': datetime.now().isoformat(),
                'model_used': self.llm_model
            }
        }

    # ------------------------------------------------------------------
    # Context building
    # ------------------------------------------------------------------

    def _build_context(self, summaries: List[Dict[str, Any]]) -> str:
        """Build combined context from multiple podcast summaries."""
        sections = []
        for i, s in enumerate(summaries, 1):
            episode_title = s.get('episode_title', '')
            podcast_title = s.get('podcast_title', '')
            summary = s.get('full_summary', '')
            key_topics = s.get('key_topics', [])
            themes = s.get('themes', [])
            quotes = s.get('quotes', [])
            companies = s.get('startups', [])

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

    def _build_writing_prompt(self, topic: str, context: str,
                              context_posts: List[str] = None) -> str:
        """Build the initial writing prompt based on Tunguz's style guidelines."""

        style_guidelines = """
Style Guidelines (inspired by Tomasz Tunguz's approach):
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
            f"{context_section}\n"
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
            "- Actionable takeaways\n\n"
            "Focus especially on addressing the specific issues mentioned in the feedback."
        )

    # ------------------------------------------------------------------
    # Grading (uses a distinct persona to reduce self-grading bias)
    # ------------------------------------------------------------------

    def _grade_blog_post(self, blog_post: str) -> Dict[str, Any]:
        """Grade blog post like an AP English teacher (Tunguz's innovation).

        Uses a strict evaluator persona distinct from the writer persona
        to reduce self-grading bias.
        """

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
            "Format your response EXACTLY as:\n"
            "GRADE: [Letter Grade]\n"
            "SCORE: [Numerical Score]\n"
            "FEEDBACK: [Detailed feedback with specific suggestions for improvement]"
        )

        response = self._generate_with_llm(
            grading_prompt,
            system="You are a strict AP English teacher and writing critic. "
                   "You grade rigorously and are harder to impress than most readers. "
                   "Be specific about weaknesses and provide actionable improvement suggestions."
        )

        return self._parse_grade(response)

    def _parse_grade(self, response: str) -> Dict[str, Any]:
        """Parse grade, score, and feedback from grader response.

        Falls back gracefully when the LLM doesn't follow the format exactly.
        """
        grade_match = re.search(r'GRADE:\s*([A-F][+-]?)', response, re.IGNORECASE)
        score_match = re.search(r'SCORE:\s*(\d+(?:\.\d+)?)', response, re.IGNORECASE)
        feedback_match = re.search(r'FEEDBACK:\s*(.*)', response, re.DOTALL | re.IGNORECASE)

        grade = grade_match.group(1).upper() if grade_match else None
        score = float(score_match.group(1)) if score_match else None
        feedback = feedback_match.group(1).strip() if feedback_match else response

        # If parsing failed, don't pretend we got a low grade — signal unknown
        if score is None:
            logger.warning("Could not parse score from grader response, defaulting to 0 (will retry)")
            score = 0.0
        if grade is None:
            logger.warning("Could not parse letter grade from grader response")
            grade = "?"

        return {
            'grade': grade,
            'score': score,
            'feedback': feedback,
            'raw_response': response
        }

    # ------------------------------------------------------------------
    # LLM interface
    # ------------------------------------------------------------------

    def _generate_with_llm(self, prompt: str,
                           system: str = "You are an expert blog writer and writing instructor.") -> str:
        """Generate text using configured LLM. Raises on failure."""
        if not OLLAMA_AVAILABLE:
            raise RuntimeError("Ollama is not installed — cannot generate blog content")

        try:
            response = ollama.chat(
                model=self.llm_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt}
                ]
            )
            return response['message']['content'].strip()
        except Exception as e:
            raise RuntimeError(f"LLM generation failed: {e}") from e

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def save_blog_post(self, blog_result: Dict[str, Any], output_dir: str = "blog_posts") -> str:
        """Save generated blog post to file."""
        output_path = Path(output_dir)
        output_path.mkdir(exist_ok=True)

        date_str = datetime.now().strftime('%Y-%m-%d')
        filename = f"{date_str}-{blog_result['slug']}.md"
        file_path = output_path / filename

        source_count = blog_result['metadata'].get('source_count', 1)
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
inspired_by: "Tomasz Tunguz's AP English grading system"
---

# {blog_result['topic']}

{blog_result['final_post']}

---

## Generation Notes

- **Final Grade**: {blog_result['final_grade']} ({blog_result['final_score']}/100)
- **Iterations**: {len(blog_result['iterations'])}
- **Source**: {source_line}
- **Generated**: {blog_result['metadata']['generated_at']}

### Grading History
"""

        for iteration in blog_result['iterations']:
            content += f"""
**Iteration {iteration['iteration']}**: {iteration['grade']} ({iteration['score']}/100)
{iteration['feedback'][:200]}...

"""

        with open(file_path, 'w') as f:
            f.write(content)

        return str(file_path)

    # ------------------------------------------------------------------
    # Social media
    # ------------------------------------------------------------------

    def generate_social_posts(self, blog_result: Dict[str, Any]) -> Dict[str, List[str]]:
        """Generate social media posts from blog content (Tunguz's feature)."""

        blog_post = blog_result['final_post']
        topic = blog_result['topic']

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
            return {'twitter': [], 'linkedin': [], 'quotes': [], 'insights': []}

        twitter_posts = self._parse_numbered_list(twitter_response)
        linkedin_posts = self._parse_numbered_list(linkedin_response)

        # Extract quotable excerpts from the blog post
        quotes = []
        insights = []
        sentences = blog_post.split('. ')
        for sentence in sentences:
            stripped = sentence.strip()
            if 50 < len(stripped) < 280:
                if any(word in stripped.lower() for word in ['key', 'important', 'crucial', 'insight']):
                    insights.append(stripped + '.')
                elif '"' in stripped:
                    quotes.append(stripped)

        return {
            'twitter': twitter_posts,
            'linkedin': linkedin_posts,
            'quotes': quotes[:3],
            'insights': insights[:5]
        }

    @staticmethod
    def _parse_numbered_list(text: str) -> List[str]:
        """Parse a numbered list from LLM output.

        Handles formats like '1. ...', '1) ...', 'POST 1: ...' etc.
        """
        pattern = r'(?:^|\n)\s*(?:\d+[\.\)]\s*|POST\s*\d+\s*:\s*)'
        items = re.split(pattern, text)
        # The first element is whatever came before the first number — usually empty
        return [item.strip() for item in items if item.strip()]

    @staticmethod
    def _generate_slug(topic: str) -> str:
        """Generate URL-friendly slug from topic."""
        slug = re.sub(r'[^\w\s-]', '', topic.lower())
        slug = re.sub(r'[-\s]+', '-', slug)
        return slug.strip('-')
