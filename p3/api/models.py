"""Pydantic models for API request/response schemas."""

from datetime import datetime, date
from typing import List, Optional, Any
from pydantic import BaseModel, Field


# ------------------------------------------------------------------
# Podcasts
# ------------------------------------------------------------------

class PodcastCreate(BaseModel):
    url: str = Field(..., description="RSS feed URL or Apple Podcasts link")
    name: Optional[str] = Field(None, description="Display name (auto-detected from feed if omitted)")
    category: Optional[str] = None


class PodcastOut(BaseModel):
    id: int
    title: str
    rss_url: str
    category: Optional[str] = None
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
    created_at: Optional[Any] = None
    episode_title: Optional[str] = None
    podcast_title: Optional[str] = None


# ------------------------------------------------------------------
# Jobs
# ------------------------------------------------------------------

class JobOut(BaseModel):
    id: str
    episode_id: Optional[int] = None
    job_type: str
    status: str
    progress: float = 0.0
    message: Optional[str] = None
    error: Optional[str] = None
    created_at: Optional[Any] = None
    started_at: Optional[Any] = None
    completed_at: Optional[Any] = None


# ------------------------------------------------------------------
# Blog posts
# ------------------------------------------------------------------

class BlogCreate(BaseModel):
    topic: str
    date: Optional[str] = Field(None, description="YYYY-MM-DD, defaults to today")
    target_grade: float = 91.0


class BlogOut(BaseModel):
    slug: str
    title: str
    date: str
    filename: str
    final_grade: Optional[str] = None
    final_score: Optional[float] = None
    content: Optional[str] = None


# ------------------------------------------------------------------
# Pipeline actions
# ------------------------------------------------------------------

class PipelineAction(BaseModel):
    """Trigger a pipeline step for an episode."""
    pass  # no body needed — episode_id comes from path


class FetchAction(BaseModel):
    """Trigger a fetch for a podcast."""
    max_episodes: Optional[int] = None


# ------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------

class SettingsOut(BaseModel):
    feeds: List[dict] = []
    settings: dict = {}


class SettingsUpdate(BaseModel):
    feeds: Optional[List[dict]] = None
    settings: Optional[dict] = None


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
