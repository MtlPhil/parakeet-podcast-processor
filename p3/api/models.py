"""Pydantic models for API request/response schemas."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

_PROVIDER_HELP = (
    "LLM provider override (ollama, openai, gemini). Defaults to configured settings."
)
_MODEL_HELP = "LLM model override. Defaults to the configured model for the provider."


# ------------------------------------------------------------------
# Podcasts
# ------------------------------------------------------------------


class PodcastCreate(BaseModel):
    url: str = Field(
        ...,
        description=(
            "RSS feed URL, Apple Podcasts link, or YouTube channel, video or "
            "playlist URL (a playlist adds one source per video)"
        ),
    )
    name: Optional[str] = Field(
        None, description="Display name (auto-detected from feed if omitted)"
    )
    category: Optional[str] = None
    episode_guids: Optional[List[str]] = Field(
        None,
        description=(
            "Hand-picked episode guids/video ids from a preview listing to "
            "download initially, instead of the usual top-N most recent"
        ),
    )


class BulkPodcastCreate(BaseModel):
    urls: List[str] = Field(
        ..., description="Source URLs, any kind accepted by POST /api/podcasts"
    )
    category: Optional[str] = None


class PreviewEpisode(BaseModel):
    guid: str
    title: str
    date: Optional[Any] = None
    description: Optional[str] = None
    in_library: bool = Field(
        False, description="Already in the library; picking it again does nothing"
    )


class SourcePreview(BaseModel):
    url: str
    name: Optional[str] = None
    source_type: str
    episodes: List[PreviewEpisode] = []


class PodcastUpdate(BaseModel):
    title: Optional[str] = None
    category: Optional[str] = None


class PodcastOut(BaseModel):
    id: int
    title: str
    rss_url: str
    category: Optional[str] = None
    source_type: str = "rss"
    created_at: Optional[Any] = None
    episode_count: Optional[int] = None


# ------------------------------------------------------------------
# Episodes
# ------------------------------------------------------------------


class EpisodeOut(BaseModel):
    id: int
    podcast_id: int
    title: str
    date: Optional[Any] = None
    url: str
    file_path: Optional[str] = None
    duration_seconds: Optional[int] = None
    status: str
    language: Optional[str] = None
    created_at: Optional[Any] = None
    podcast_title: Optional[str] = None


# ------------------------------------------------------------------
# Transcripts
# ------------------------------------------------------------------


class TranscriptSegment(BaseModel):
    id: int
    episode_id: int
    speaker: Optional[str] = None
    timestamp_start: Optional[float] = None
    timestamp_end: Optional[float] = None
    text: str
    confidence: Optional[float] = None


# ------------------------------------------------------------------
# Summaries
# ------------------------------------------------------------------


class SummaryOut(BaseModel):
    id: int
    episode_id: int
    key_topics: List[str] = []
    themes: List[str] = []
    quotes: List[str] = []
    startups: List[str] = []
    digest_date: Optional[Any] = None
    full_summary: Optional[str] = None
    long_summary: Optional[str] = None
    # Quebec French versions; None until translated
    key_topics_fr: Optional[List[str]] = None
    themes_fr: Optional[List[str]] = None
    full_summary_fr: Optional[str] = None
    long_summary_fr: Optional[str] = None
    created_at: Optional[Any] = None
    episode_title: Optional[str] = None
    podcast_title: Optional[str] = None


# ------------------------------------------------------------------
# Jobs
# ------------------------------------------------------------------


class JobOut(BaseModel):
    id: str
    episode_id: Optional[int] = None
    podcast_id: Optional[int] = None
    job_type: str
    status: str
    progress: float = 0.0
    message: Optional[str] = None
    error: Optional[str] = None
    created_at: Optional[Any] = None
    started_at: Optional[Any] = None
    completed_at: Optional[Any] = None
    episode_title: Optional[str] = None
    podcast_title: Optional[str] = None


class DailyRunFailure(BaseModel):
    job_id: str
    job_type: str
    podcast_title: Optional[str] = None
    episode_title: Optional[str] = None
    status: str = "failed"
    error: Optional[str] = None


class DailyRunOut(BaseModel):
    run_id: str
    started_at: Optional[str] = None
    state: str = Field(..., description="running, finished or interrupted")
    new_episodes: int = 0
    jobs: Dict[str, Dict[str, int]] = Field(
        default_factory=dict, description="Job counts by type, then by status"
    )
    failures: List[DailyRunFailure] = []
    export_message: Optional[str] = None


# ------------------------------------------------------------------
# Search
# ------------------------------------------------------------------


class SearchSegment(BaseModel):
    start: Optional[float] = None
    text: str


class SearchResult(BaseModel):
    episode_id: int
    episode_title: str
    podcast_id: int
    podcast_title: str
    date: Optional[Any] = None
    status: Optional[str] = None
    title_match: bool = False
    summary_snippet: Optional[str] = None
    segment_count: int = 0
    segments: List[SearchSegment] = []


# ------------------------------------------------------------------
# Blog posts
# ------------------------------------------------------------------


class BlogCreate(BaseModel):
    topic: str
    date: Optional[str] = Field(None, description="YYYY-MM-DD, defaults to today")
    target_grade: float = 91.0
    provider: Optional[str] = Field(None, description=_PROVIDER_HELP)
    model: Optional[str] = Field(None, description=_MODEL_HELP)


class BlogOut(BaseModel):
    slug: str
    title: str
    date: str
    filename: str
    final_grade: Optional[str] = None
    final_score: Optional[float] = None
    content: Optional[str] = None
    content_fr: Optional[str] = Field(
        None, description="Quebec French version; None for posts made before it"
    )


class LinkedInCreate(BaseModel):
    episode_id: int
    provider: Optional[str] = Field(None, description=_PROVIDER_HELP)
    model: Optional[str] = Field(None, description=_MODEL_HELP)


class SynopsisCreate(BaseModel):
    provider: Optional[str] = Field(None, description=_PROVIDER_HELP)
    model: Optional[str] = Field(None, description=_MODEL_HELP)


class SynopsisEmail(BaseModel):
    to: str = Field(..., description="Email addresses, separated by commas")
    language: Optional[str] = Field(
        None, description="en or fr. Defaults to the user's language."
    )


class LinkedInOut(BaseModel):
    slug: str
    title: str
    date: str
    filename: str
    episode_title: Optional[str] = None
    podcast_title: Optional[str] = None
    content: Optional[str] = None
    content_fr: Optional[str] = None


# ------------------------------------------------------------------
# Pipeline actions
# ------------------------------------------------------------------


class FetchAction(BaseModel):
    """Trigger a fetch for a podcast."""

    max_episodes: Optional[int] = None
    episode_guids: Optional[List[str]] = Field(
        None,
        description=(
            "Hand-picked episode guids/video ids (from GET "
            "/api/podcasts/{id}/available) to download instead of the top-N"
        ),
    )


# ------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------


class SettingsOut(BaseModel):
    feeds: List[dict] = []
    settings: dict = {}


class SettingsUpdate(BaseModel):
    feeds: Optional[List[dict]] = None
    settings: Optional[dict] = None


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    thread_id: Optional[str] = Field(
        None, description="Continue this conversation (its earlier turns give context)"
    )


class AskRating(BaseModel):
    rating: int = Field(..., ge=-1, le=1, description="1 helpful, -1 not, 0 clear")


class UserSettings(BaseModel):
    """A user's own settings (see deps.USER_SETTING_DEFAULTS). Omitted or
    null fields fall back to the shared settings."""

    language: Optional[str] = Field(None, pattern="^(en|fr)$")
    tone: Optional[str] = None
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    gemini_model: Optional[str] = None
    max_episodes_per_feed: Optional[int] = Field(None, ge=1, le=100)
    daily_run_time: Optional[str] = None
    daily_run_linkedin: Optional[bool] = None
    daily_run_export: Optional[bool] = None
    daily_run_email: Optional[bool] = None
    email: Optional[str] = None


class MeOut(BaseModel):
    username: str
    display_name: Optional[str] = None
    email: Optional[str] = None
    role: str
    settings: dict = Field(
        default_factory=dict, description="Effective settings, with fallbacks"
    )
    own_settings: dict = Field(
        default_factory=dict, description="Only what the user set themselves"
    )


class DeletedEpisodeOut(BaseModel):
    url: str
    title: Optional[str] = None
    podcast_title: Optional[str] = None
    deleted_at: Optional[Any] = None


class DeletedEpisodeAllow(BaseModel):
    url: str


class UserOut(BaseModel):
    username: str
    display_name: Optional[str] = None
    email: Optional[str] = None
    role: str
    created_at: Optional[Any] = None


class RoleUpdate(BaseModel):
    role: str = Field(..., pattern="^(admin|user)$")


# ------------------------------------------------------------------
# Stats
# ------------------------------------------------------------------


class StatsOut(BaseModel):
    total_podcasts: int = 0
    total_episodes: int = 0
    episodes_downloaded: int = 0
    episodes_transcribed: int = 0
    episodes_processed: int = 0
    total_summaries: int = 0
    active_jobs: int = 0
    running_jobs: int = 0
    queued_jobs: int = 0
