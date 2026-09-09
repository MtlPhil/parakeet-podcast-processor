# Frontend Plan for Parakeet Podcast Processor (P3)

## Gaps & Suggestions (not captured in original requirements)

Before the architecture — here are things missing from the plan that a usable frontend will need:

1. **Background job processing** — Transcription takes 5-30min per episode, downloads take 30s-5min, LLM calls 1-3min. Every pipeline step needs to run asynchronously with progress tracking. Without this, the browser will time out or the UI will freeze.

2. **Real-time progress updates** — Users need to see "downloading... 45%", "transcribing... segment 12/50", not just a spinner. Server-Sent Events (SSE) or WebSocket required.

3. **Pipeline orchestration** — The ability to run the full pipeline (fetch → transcribe → digest → export) as one action, or run individual steps. Show a pipeline status view per episode.

4. **Dashboard / overview page** — A landing page showing: recent activity, episodes in progress, completed digests, quick stats.

5. **Error handling & retry** — When a pipeline step fails (Ollama down, ffmpeg error, network timeout), the UI should show the error and offer a retry button per step.

6. **Settings / configuration UI** — Manage `feeds.yaml` settings through the UI: LLM provider, model, max episodes, Parakeet vs Whisper toggle. Avoids requiring users to edit YAML.

7. **Search** — Search across transcripts, summaries, blog posts. Becomes essential once you have 50+ episodes.

8. **Export / download from UI** — Download markdown, JSON, blog posts as files directly from the browser.

9. **Responsive design** — Mobile-friendly for checking podcast processing status on the go.

10. **API layer** — A proper REST API between the frontend and P3 backend. The existing code is all synchronous class methods — needs an API wrapper.

---

## Tech Stack Decision

### Backend API: **FastAPI**
- Async support for long-running tasks without blocking
- Built-in OpenAPI docs (useful for debugging)
- Pydantic models for request/response validation
- Lightweight — fits the project's "local tool" ethos
- Python-native — reuses existing P3 modules directly

### Background Tasks: **FastAPI BackgroundTasks + SQLite job tracking**
- For a local tool, Celery + Redis is overkill
- FastAPI's `BackgroundTasks` for fire-and-forget
- Track job status in a `jobs` table in the existing DuckDB database
- Poll `/api/jobs/{id}` for progress (SSE can be added later)

### Frontend: **React + Vite + Tailwind CSS**
- React for component-based UI with good state management
- Vite for fast dev server and builds
- Tailwind for rapid styling without custom CSS
- SPA that talks to the FastAPI backend

### Why not server-rendered (Jinja2/HTMX)?
- Pipeline progress tracking needs dynamic updates that SSR handles poorly
- Multiple async operations per page (job status, podcast list, episode details)
- React's state management is a better fit for this complexity

---

## Architecture

```
┌──────────────────────────────────────────────────┐
│                   React SPA                       │
│  (Vite + Tailwind)                                │
│                                                   │
│  Pages: Dashboard, AddPodcast, PodcastDetail,     │
│         EpisodeDetail, BlogPosts, Settings        │
└───────────────────┬──────────────────────────────┘
                    │ HTTP (fetch)
                    ▼
┌──────────────────────────────────────────────────┐
│              FastAPI Backend                       │
│                                                   │
│  /api/podcasts     CRUD + feed management         │
│  /api/episodes     list, detail, status           │
│  /api/jobs         pipeline job tracking          │
│  /api/transcripts  view transcript segments       │
│  /api/summaries    view structured summaries      │
│  /api/exports      download md/json/html          │
│  /api/blogs        list and view blog posts       │
│  /api/settings     read/write config              │
│                                                   │
│  Background tasks → existing P3 modules           │
└───────────────────┬──────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────────┐
│  Existing P3 Modules (unchanged)                  │
│                                                   │
│  PodcastDownloader, AudioTranscriber,             │
│  TranscriptCleaner, DigestExporter,               │
│  BlogWriter, P3Database                           │
└───────────────────┬──────────────────────────────┘
                    │
                    ▼
              DuckDB (data/p3.duckdb)
```

---

## Database Changes

Add a `jobs` table to track background pipeline operations:

```sql
CREATE TABLE IF NOT EXISTS jobs (
    id VARCHAR PRIMARY KEY,          -- UUID
    episode_id INTEGER REFERENCES episodes(id),
    job_type VARCHAR NOT NULL,       -- 'fetch', 'transcribe', 'digest', 'export', 'write', 'full_pipeline'
    status VARCHAR DEFAULT 'pending', -- 'pending', 'running', 'completed', 'failed'
    progress REAL DEFAULT 0.0,       -- 0.0 to 1.0
    message VARCHAR,                 -- current step description
    error TEXT,                      -- error message if failed
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_episode_id ON jobs(episode_id);
```

---

## File Structure

```
p3/
├── api/                          # NEW — FastAPI backend
│   ├── __init__.py
│   ├── main.py                   # FastAPI app, CORS, lifespan
│   ├── deps.py                   # Shared dependencies (db, config)
│   ├── models.py                 # Pydantic request/response models
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── podcasts.py           # CRUD for podcasts + trigger fetch
│   │   ├── episodes.py           # List/detail episodes
│   │   ├── jobs.py               # Job status polling
│   │   ├── transcripts.py        # View transcript segments
│   │   ├── summaries.py          # View summaries
│   │   ├── exports.py            # Download exports
│   │   ├── blogs.py              # List/view blog posts
│   │   └── settings.py           # Config management
│   └── tasks.py                  # Background task wrappers around P3 modules
│
├── frontend/                     # NEW — React SPA
│   ├── package.json
│   ├── vite.config.js
│   ├── index.html
│   ├── src/
│   │   ├── main.jsx
│   │   ├── App.jsx
│   │   ├── api/                  # API client functions
│   │   │   └── client.js
│   │   ├── components/
│   │   │   ├── Layout.jsx        # Shell: sidebar + content area
│   │   │   ├── PodcastCard.jsx
│   │   │   ├── EpisodeRow.jsx
│   │   │   ├── PipelineStatus.jsx  # Progress indicator per step
│   │   │   ├── TranscriptViewer.jsx
│   │   │   ├── SummaryCard.jsx
│   │   │   └── BlogPreview.jsx
│   │   ├── pages/
│   │   │   ├── Dashboard.jsx
│   │   │   ├── AddPodcast.jsx
│   │   │   ├── PodcastDetail.jsx
│   │   │   ├── EpisodeDetail.jsx
│   │   │   ├── BlogPosts.jsx
│   │   │   └── Settings.jsx
│   │   └── hooks/
│   │       └── useJobPoller.js   # Poll job status at interval
│   └── tailwind.config.js
│
├── p3/                           # EXISTING — unchanged
│   ├── cli.py
│   ├── database.py
│   ├── downloader.py
│   ├── transcriber.py
│   ├── cleaner.py
│   ├── exporter.py
│   └── writer.py
│
├── tests/                        # EXISTING + new API tests
│   ├── test_database.py
│   ├── test_api_podcasts.py      # NEW
│   ├── test_api_episodes.py      # NEW
│   └── test_api_jobs.py          # NEW
│
└── pyproject.toml                # Add fastapi, uvicorn deps
```

---

## API Endpoints (Phase 1)

### Podcasts
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/podcasts` | Add podcast by RSS URL, triggers fetch job |
| GET | `/api/podcasts` | List all podcasts |
| GET | `/api/podcasts/{id}` | Podcast detail with episodes |
| DELETE | `/api/podcasts/{id}` | Remove podcast and its data |

### Episodes
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/episodes` | List episodes (filter by podcast_id, status) |
| GET | `/api/episodes/{id}` | Episode detail (status, transcript, summary) |
| POST | `/api/episodes/{id}/transcribe` | Trigger transcription job |
| POST | `/api/episodes/{id}/digest` | Trigger summarization job |
| POST | `/api/episodes/{id}/pipeline` | Run full pipeline on episode |

### Jobs
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/jobs` | List recent jobs |
| GET | `/api/jobs/{id}` | Job status + progress |

### Transcripts
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/episodes/{id}/transcript` | Full transcript with timestamps |

### Summaries
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/episodes/{id}/summary` | Structured summary (topics, themes, quotes) |

### Exports
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/exports/{date}` | Download digest for date (format=md\|json) |
| POST | `/api/exports` | Generate export for date |

### Blogs
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/blogs` | List generated blog posts |
| GET | `/api/blogs/{slug}` | View blog post content |
| POST | `/api/blogs` | Generate new blog post (topic, date, target_grade) |

### Settings
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/settings` | Current configuration |
| PUT | `/api/settings` | Update configuration |

---

## Frontend Pages (Phase 1)

### Dashboard
- Stats bar: total podcasts, episodes, pending transcriptions, recent blogs
- Recent activity feed (last 10 completed jobs)
- Active jobs with progress bars
- Quick action: "Add Podcast" button

### Add Podcast
- Form: RSS URL input
- On submit: POST to /api/podcasts → starts fetch job
- Show progress as episodes are discovered and downloaded
- Redirect to podcast detail when done

### Podcast Detail
- Podcast title, category, RSS URL, date added
- Episode list table: title, date, status badge (downloaded/transcribed/processed)
- Bulk actions: "Transcribe All", "Digest All"
- Click episode → Episode Detail

### Episode Detail
- Episode metadata (title, date, podcast, duration)
- Pipeline status: 4-step progress bar (downloaded → transcribed → processed → exported)
- Action buttons: "Transcribe", "Summarize", "Generate Blog" (enabled based on status)
- Tabs:
  - **Transcript** — timestamped segments, scrollable
  - **Summary** — topics, themes, quotes, companies in cards
  - **Blog Post** — rendered markdown with grade history

### Blog Posts
- List of generated blogs with title, date, grade, source podcast
- Click → full rendered blog post
- "Generate New" button → form with topic, date, target grade

### Settings
- LLM provider toggle (Ollama / OpenAI)
- Model name input
- Parakeet vs Whisper toggle
- Max episodes per feed
- Save button → PUT /api/settings

---

## Implementation Order

### Step 1: FastAPI Backend Shell
- Create `p3/api/` package structure
- FastAPI app with CORS, lifespan (db connection)
- Dependencies module (shared db instance, config loader)
- Pydantic models for all request/response types
- Jobs table in database.py

### Step 2: Podcast & Episode API Routes
- CRUD routes for podcasts
- Episode listing and detail routes
- Wire up to existing P3Database methods

### Step 3: Background Task System
- tasks.py wrapping P3 modules with job tracking
- Job create/update/query in database
- Progress callback integration

### Step 4: Pipeline API Routes
- Transcribe, digest, export, write endpoints
- Each triggers a background task, returns job ID
- Jobs polling endpoint

### Step 5: Transcript, Summary, Export, Blog Routes
- Read-only routes returning stored data
- Export download endpoint (file response)
- Blog listing and detail

### Step 6: Settings Route
- Read/write feeds.yaml through API

### Step 7: React Frontend Shell
- Vite + React + Tailwind setup
- Layout component (sidebar navigation)
- API client module
- Router setup for all pages

### Step 8: Dashboard Page
- Stats fetching from API
- Active jobs list with polling
- Recent activity

### Step 9: Add Podcast + Podcast Detail Pages
- Add podcast form with job progress
- Podcast detail with episode table
- Bulk action buttons

### Step 10: Episode Detail Page
- Pipeline status visualization
- Action buttons triggering jobs
- Transcript/Summary/Blog tabs

### Step 11: Blog Posts + Settings Pages
- Blog list and detail views
- Settings form

### Step 12: API Tests
- TestClient-based tests for all API routes
- Job lifecycle tests

---

## Phase 2 Items (deferred)

- **Authentication**: JWT-based auth with FastAPI middleware, login page in React
- **Browse & List Podcasts**: Public podcast directory / search by category
- **SSE/WebSocket**: Replace polling with real-time push for job progress
- **Search**: Full-text search across transcripts and summaries
- **Multi-user support**: User-scoped data isolation
