# Parakeet Podcast Processor (P³)

P³ turns podcast feeds into searchable transcripts, structured summaries and
ready-to-edit written content. It downloads episodes from RSS, transcribes them
locally with [Parakeet MLX](https://github.com/senstella/parakeet-mlx) (or
Whisper), cleans the transcript, extracts topics, themes, quotes and companies
with an LLM, and exports daily digests, blog posts and LinkedIn posts.

It is built to run entirely on an Apple Silicon Mac: transcription happens on
the GPU through MLX and the default LLM backend is a local
[Ollama](https://ollama.com) model, so no audio or transcript has to leave the
machine. OpenAI and Gemini are supported as optional hosted backends.

P³ ships as both a command-line tool (`p3`) and a web app (FastAPI + React).

---

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Using the CLI](#using-the-cli)
- [Using the web app](#using-the-web-app)
- [Deployment](#deployment)
- [Architecture](#architecture)
- [Development](#development)
- [Acknowledgements](#acknowledgements)

---

## Features

- **Feed ingestion.** Add podcasts by RSS URL or Apple Podcasts link. New
  episodes are downloaded with retry and backoff and normalized to 16 kHz mono
  audio with `ffmpeg`.
- **Local transcription.** Parakeet MLX on Apple Silicon, with automatic fallback
  to OpenAI Whisper. Long episodes are transcribed in 10-minute chunks to keep
  memory use bounded.
- **Transcript cleaning.** Filler words are stripped with a regex. The LLM then
  polishes the transcript and removes ads and sponsor reads. Chunks are split at
  natural pauses in the audio, which tends to hand an ad break to the model as a
  self-contained block.
- **Structured digests.** Each episode gets a ~300-word summary plus lists of
  key topics, themes, notable quotes and companies mentioned. If no LLM is
  available, a keyword-frequency fallback fills them in.
- **Long-form synopsis.** On demand, P³ writes detailed study notes for an
  episode, one section of the transcript at a time.
- **Writing tools.**
  - *Blog posts* built from a day's digests with an iterative write → grade →
    revise loop. A separate "strict AP English teacher" persona scores each
    draft until it reaches a target score (91/100 by default, up to 3 rounds).
  - *LinkedIn posts* for a single episode in English and in Quebec French.
  - *Social snippets* (Twitter/LinkedIn) generated from a finished blog post.
- **Exports.** Daily digests as Markdown, JSON or email-ready HTML, plus
  per-episode transcripts as Markdown (single file or a zip per podcast).
- **Web app.** Dashboard, podcast and episode pages, transcript and summary
  views, content generation, settings, and live job progress.
- **Safe background processing.** Heavy work runs through a single serial job
  queue, so only one transcription or LLM job uses the machine at a time.

## How it works

```
 RSS / Apple link
        │
        ▼
 ┌─────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
 │  Download   │──▶│  Transcribe  │──▶│    Digest    │──▶│    Export    │
 │ feedparser  │   │ Parakeet MLX │   │ clean + LLM  │   │ MD/JSON/HTML │
 │  + ffmpeg   │   │  / Whisper   │   │  summarize   │   │ blog/LinkedIn│
 └─────────────┘   └──────────────┘   └──────────────┘   └──────────────┘
        │                 │                  │                  │
        └─────────────────┴──── DuckDB ──────┴──────────────────┘
```

Each episode moves through three states stored in DuckDB:

| Status        | Set by                          | Meaning                                  |
|---------------|---------------------------------|------------------------------------------|
| `downloaded`  | `p3 fetch` / fetch job          | Normalized audio is on disk              |
| `transcribed` | `p3 transcribe` / transcribe job | Timestamped segments are stored          |
| `processed`   | `p3 digest` / digest job        | Summary and structured fields are stored |

Every step is idempotent. Re-transcribing or re-digesting an episode replaces
its rows instead of duplicating them, and re-digesting keeps any synopsis that
was already generated.

## Requirements

| Requirement | Notes |
|---|---|
| macOS on Apple Silicon (M1 or later) | Needed for Parakeet MLX, which is only installed on that platform. Elsewhere Whisper is used (untested). |
| Python 3.10+ | |
| [ffmpeg](https://ffmpeg.org) | Audio normalization and chunking: `brew install ffmpeg` |
| [Ollama](https://ollama.com) | Default local LLM backend. Optional if you use OpenAI or Gemini. |
| Node.js 20.19+ and npm | Only needed to build or develop the web frontend |

Whisper and Parakeet models are downloaded automatically on first use (a few
hundred MB each). Plan for several GB of free memory while a model is loaded.

## Installation

```bash
# 1. System tools
brew install ffmpeg
brew install ollama          # or download the app from https://ollama.com
ollama pull llama3.2         # any chat model works; set it in config

# 2. Python package
git clone https://github.com/<your-account>/parakeet-podcast-processor.git
cd parakeet-podcast-processor
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[web]"      # CLI + web API; use -e . for the CLI only

# 3. Initialize directories, config and the database
p3 init
```

`p3 init` checks for `ffmpeg` and `ollama` and creates `data/`, `exports/`,
`blog_posts/` and `linkedin_posts/`. It also copies
`config/feeds.yaml.example` to `config/feeds.yaml` and creates the DuckDB
database at `data/p3.duckdb`.

To use the web app, also build the frontend:

```bash
cd frontend
npm install
npm run build                # outputs frontend/dist, served by the API
cd ..
```

## Configuration

All settings live in `config/feeds.yaml`. The file is gitignored, so your feed
list stays local. The web app's **Settings** page edits the same file.

```yaml
feeds:
  - name: "Lenny's Podcast"
    url: "https://api.substack.com/feed/podcast/10845.rss"
    category: "product"

settings:
  max_episodes_per_feed: 3
  audio_format: wav                 # wav or mp3

  parakeet_enabled: true            # false → Whisper
  parakeet_model: "mlx-community/parakeet-tdt-0.6b-v2"
  whisper_model: base               # tiny | base | small | medium | large

  llm_provider: ollama              # ollama | openai | gemini
  llm_model: "llama3.2:latest"      # model for ollama/openai
  ollama_base_url: "http://localhost:11434"
  gemini_model: "gemini-flash-3.6"  # model used when the provider is gemini

  export_format: [markdown, json]   # defaults for `p3 export`
```

### LLM providers

| Provider | Credentials | Notes |
|---|---|---|
| `ollama` | none | Runs locally. Models are unloaded shortly after use to free memory. |
| `openai` | `OPENAI_API_KEY` environment variable | Any chat-completions model, e.g. `gpt-4o-mini`. |
| `gemini` | `GEMINI_API_KEY` environment variable, or the key alone in `config/gemini_api_key.txt` (gitignored) | Uses `gemini_model`. |

In the web app, the synopsis, blog and LinkedIn actions can also switch to
Gemini for a single request without changing the default.

If a hosted provider is selected but no key is found, digests fall back to
basic keyword extraction instead of failing.

## Using the CLI

Global options: `--config PATH`, `--db PATH`, `-v` (debug logging), `-q`
(warnings only).

| Command | Description |
|---|---|
| `p3 init` | Create directories, config and database; check prerequisites |
| `p3 fetch [--max-episodes N] [--dry-run]` | Download new episodes from every configured feed |
| `p3 transcribe [--episode-id ID] [--model NAME]` | Transcribe all `downloaded` episodes, or one |
| `p3 digest [--episode-id ID] [--provider P] [--model M]` | Clean and summarize all `transcribed` episodes, or one |
| `p3 export [--date YYYY-MM-DD] [--format markdown\|json\|html] [--output PATH]` | Export the digest for a date (default: today) to `exports/` |
| `p3 export-transcript [--date YYYY-MM-DD] [--format txt\|markdown]` | Export transcripts for a date, or for one episode (prompted) |
| `p3 write --topic "…" [--date …] [--target-grade 91] [--dry-run]` | Write a graded blog post from a day's digests, plus social snippets |
| `p3 status` | Count episodes by pipeline status |

A typical daily run:

```bash
p3 fetch && p3 transcribe && p3 digest && p3 export
p3 write --topic "What founders are saying about AI agents"
```

Digests are grouped by the date they were generated, so `p3 export` and
`p3 write` with no `--date` use whatever was digested today.

## Using the web app

For development, run the API and the Vite dev server side by side:

```bash
uvicorn p3.api.main:app --reload     # API on http://127.0.0.1:8000
cd frontend && npm run dev           # UI on http://localhost:5173 (proxies /api)
```

The app's pages:

- **Dashboard**: pipeline counts, one-click "transcribe / digest / run
  everything" for the whole library, and a paginated job history. Failed
  fetch, transcribe, digest and pipeline jobs can be retried.
- **Podcasts**: add a feed (RSS or Apple Podcasts link), fetch new episodes,
  run steps for every episode of a podcast, and download all transcripts as a
  zip.
- **Episode**: run each step, read the transcript and summary, generate the
  long-form synopsis, and download the transcript.
- **Content**: generate LinkedIn posts (pick a podcast, then an episode) or
  themed blog posts from a day's digests. Browse everything generated so far.
- **Settings**: edit provider, models and download options.

Interactive API docs are available at `http://127.0.0.1:8000/docs`.

## Deployment

P³ is designed as a single-user tool that runs on your own Mac. In production,
one `uvicorn` process serves both the API and the built frontend.

```bash
cd frontend && npm run build && cd ..
uvicorn p3.api.main:app --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000
```

Run it from the repository root. Data, config and output paths are relative to
the working directory.

> **Security:** the API has no authentication, and anyone who can reach it can
> change settings, start jobs and delete podcasts. State-changing requests
> from other websites' pages are rejected by an Origin check, but that is not
> a substitute for authentication. Keep it bound to `127.0.0.1`.
> For access from other devices, put it behind something that authenticates,
> such as a VPN like Tailscale or a reverse proxy with auth. Do not expose
> port 8000 directly.

### Running as a background service (launchd)

To start P³ at login and keep it running, save the following as
`~/Library/LaunchAgents/com.p3.server.plist`, adjusting the paths:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.p3.server</string>
  <key>WorkingDirectory</key><string>/path/to/parakeet-podcast-processor</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/parakeet-podcast-processor/.venv/bin/uvicorn</string>
    <string>p3.api.main:app</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>8000</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/p3.log</string>
  <key>StandardErrorPath</key><string>/tmp/p3.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.p3.server.plist
```

`PATH` must include Homebrew so the server can find `ffmpeg`. Add
`OPENAI_API_KEY` or `GEMINI_API_KEY` under `EnvironmentVariables` if you use a
hosted provider.

### Exposing the dev server on a LAN

The Vite dev server listens on `localhost` only by default. To reach it from
another device, create `frontend/.env.local` (gitignored):

```bash
P3_DEV_HOST=0.0.0.0
P3_ALLOWED_HOSTS=p3.example.com   # optional, comma-separated extra hostnames
```

The same security caveat applies.

### Scheduling the CLI

Without the web app, the pipeline can run on a schedule with cron or launchd:

```cron
0 6 * * * cd /path/to/parakeet-podcast-processor && .venv/bin/p3 -q fetch && .venv/bin/p3 -q transcribe && .venv/bin/p3 -q digest && .venv/bin/p3 -q export
```

## Architecture

```
p3/
├── cli.py            Click command group (`p3 …`)
├── downloader.py     RSS parsing, download with retry, ffmpeg normalization
├── url_resolver.py   Apple Podcasts link → RSS feed via the iTunes Lookup API
├── transcriber.py    Parakeet MLX (chunked) with Whisper fallback
├── cleaner.py        Transcript cleaning, digests, long-form synopsis
├── writer.py         Graded blog posts, LinkedIn posts, social snippets
├── exporter.py       Markdown / JSON / HTML formatting
├── llm.py            Ollama / OpenAI / Gemini chat client and provider resolution
├── database.py       DuckDB storage layer
└── api/
    ├── main.py       FastAPI app, lifespan, static frontend
    ├── deps.py       Shared database instance and config I/O
    ├── job_queue.py  Single-threaded serial job runner
    ├── tasks.py      Job wrappers around the pipeline modules
    ├── models.py     Pydantic request/response schemas
    └── routers/      podcasts, episodes, jobs, transcripts, summaries,
                      exports, blogs, linkedin, settings
frontend/             React + Vite + Tailwind single-page app
tests/                pytest suite (no external services required)
```

### Data model

DuckDB file at `data/p3.duckdb`:

- `podcasts`: feed title, RSS URL (unique), category
- `episodes`: title, publish date, audio URL (unique), local file path, status
- `transcripts`: one row per segment with start/end timestamps and text
- `summaries`: short summary, long-form synopsis, and JSON lists of topics,
  themes, quotes and companies, keyed by episode and digest date
- `jobs`: background job type, status, progress, message, error, and timestamps

Rows are returned as dicts keyed by column name. Schema changes are applied
with idempotent `CREATE … IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`
statements on startup.

### Background jobs

API endpoints never do heavy work inline. They create a `jobs` row and enqueue
a task on a single worker thread (`job_queue.py`). Tasks run strictly one at a
time, because loading several ML models at once can exhaust memory on a laptop.
The frontend polls `/api/jobs/{id}` for progress. Jobs left pending or running
by a previous process are marked as failed on startup, and they can be retried.

### LLM usage

`cleaner.py` and `writer.py` share one `LLMClient` (`llm.py`). Requests are
bounded in two ways:

- **Input.** Transcripts are cut to about 100k characters, keeping the start and
  end. Cleaning and synopsis work on chunks rather than the whole transcript,
  because small local models tend to compress long inputs heavily.
- **Output.** Every request sets a token cap, and Ollama gets a mild repeat
  penalty, so a model caught in a repetition loop cannot run away.

Structured JSON is extracted from model output with code-fence stripping and
balanced-brace matching. List fields are coerced to plain strings.

### Output locations

| Output | Location |
|---|---|
| Normalized audio | `data/audio/` |
| Database | `data/p3.duckdb` |
| Digests and transcript exports | `exports/` |
| Blog posts (Markdown + YAML front matter) | `blog_posts/` |
| LinkedIn posts (English + Quebec French) | `linkedin_posts/` |

## Development

```bash
pip install -e ".[dev,web]"
PYTHONPATH=. pytest tests/ -v        # full suite, no network or models needed
```

`black`, `isort` and `mypy` are included in the `dev` extra and configured in
`pyproject.toml`, and `npm run lint` runs ESLint for the frontend. The codebase
is not yet fully clean under these tools (see [ROADMAP.md](ROADMAP.md)).

Tests cover the database layer, API endpoints (through `TestClient` against a
temporary database), the job queue, text-processing helpers in the cleaner and
writer, the exporters and the LLM client's configuration logic. Model inference
and network calls are not exercised.

See [ROADMAP.md](ROADMAP.md) for planned work.

## Acknowledgements

- The workflow, and in particular the graded writing loop, is inspired by
  [Tomasz Tunguz](https://tomtunguz.com)'s description of his podcast
  processing system on the *How I AI* podcast.
- Transcription by [NVIDIA Parakeet](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2)
  via [parakeet-mlx](https://github.com/senstella/parakeet-mlx) and
  [OpenAI Whisper](https://github.com/openai/whisper).
- Originally forked from
  [haasonsaas/parakeet-podcast-processor](https://github.com/haasonsaas/parakeet-podcast-processor).
