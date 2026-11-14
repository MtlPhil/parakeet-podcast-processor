# Parakeet Podcast Processor (P³)

P³ turns podcast feeds into searchable transcripts, structured summaries and
ready-to-edit written content. It downloads episodes from RSS feeds and YouTube, transcribes them
locally with [Parakeet MLX](https://github.com/senstella/parakeet-mlx) (or
Whisper), cleans the transcript, extracts topics, themes, quotes and companies
with an LLM, and exports daily digests, blog posts and LinkedIn posts.

It is built to run entirely on an Apple Silicon Mac: transcription happens on
the GPU through MLX and the default LLM backend is a local
[Ollama](https://ollama.com) model, so no audio or transcript has to leave the
machine. OpenAI and Gemini are supported as optional hosted backends.

**Other platforms.** On Linux and Windows, transcription uses
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) on the CPU, or
on an NVIDIA GPU if there is one; Parakeet is Apple Silicon only. This path
is new: it is covered by tests with a stand-in model, and has not yet been
run on real episodes or timed. The quickest way to try it is the
[container](#container). The one-command installer is still Mac only.

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
- [License](#license)
- [Acknowledgements](#acknowledgements)

---

## Features

- **Feed ingestion.** Add podcasts by RSS URL or Apple Podcasts link. New
  episodes are downloaded with retry and backoff and normalized to 16 kHz mono
  audio with `ffmpeg`.
- **YouTube sources.** Add a YouTube channel (new uploads are fetched like a
  feed) or a single video. A playlist URL adds each of its videos as its own
  standalone source. Audio is downloaded with
  [yt-dlp](https://github.com/yt-dlp/yt-dlp) and transcribed locally like any
  other episode; YouTube captions are not used. Shorts and livestreams (live,
  upcoming and archived streams) are skipped. Premieres are kept once they
  have aired.
- **Local transcription.** Each episode's language is detected first and
  stored. English goes to Parakeet MLX; every other language goes to Whisper
  (MLX, large-v3-turbo by default), which is told the language. Parakeet has
  no language input and drifts into English on spontaneous non-English speech,
  so it is only used for the languages listed in `parakeet_languages`. Long
  Parakeet episodes are transcribed in 10-minute chunks that overlap by 15
  seconds, to keep memory use bounded without cutting a sentence at a chunk
  boundary. If Parakeet fails, Whisper transcribes the whole episode.
- **Transcript cleaning.** Filler words are stripped with a regex. The summary
  prompts then tell the LLM to ignore ads and sponsor reads. The transcript is
  not rewritten by the LLM first: that took most of each episode's processing
  time and let sponsors through more often than the prompt rule does.
- **Structured digests.** Each episode gets a ~300-word summary plus lists of
  key topics, themes, notable quotes and companies mentioned.
- **Long-form synopsis.** On demand, P³ writes detailed study notes for an
  episode, one section of the transcript at a time.
- **Writing tools.**
  - *Blog posts* built from a day's digests with an iterative write → grade →
    revise loop. A separate "strict AP English teacher" persona scores each
    draft until it reaches a target score (91/100 by default, up to 3 rounds).
  - *LinkedIn posts* for a single episode in English and in Quebec French.
  - Every digest, synopsis and post exists in English and Quebec French.
    Each user picks the language shown first and can switch on any page.
  - *Social snippets* (Twitter/LinkedIn) generated from a finished blog post.
- **Ask.** Questions in plain language, in English or French, answered from
  your own library with every claim cited to an episode and a transcript
  moment. Passages are found by keyword (BM25) and by meaning (bge-m3
  embeddings), reranked by a cross-encoder, and the answer streams in. "How
  often" and "which episodes" questions also list every episode that
  mentions what they count. Conversations are kept, and answers can be rated.
- **Exports.** Daily digests as Markdown, JSON or email-ready HTML (the
  summaries' section titles and lists kept), plus
  per-episode transcripts as Markdown (single file or a zip per podcast).
- **Web app.** Dashboard, podcast and episode pages, transcript and summary
  views, search across transcripts and summaries, bulk add (paste URLs or
  import an OPML file), content generation, settings, and live job progress.
- **Several users.** Sign-in with P³'s own accounts, or through Authelia
  behind a reverse proxy (see [Users and sign-in](#users-and-sign-in)). Each
  user has their own podcasts, episodes, posts, writing tone and settings;
  an episode two users follow is downloaded and processed once.
- **Daily run.** Every day at 4:00 (each user can change the time or turn it
  off), the user's podcasts are fetched and new episodes transcribed and
  summarized, optionally followed by LinkedIn drafts, a digest export and an
  emailed report. A run that could read no feed (no network) is tried again
  every 15 minutes, a few times, and a report that couldn't be sent for lack
  of network goes out once it is back (within a day).
- **Disk cleanup.** An episode's audio is deleted once it is digested; the
  transcript and summary are kept.
- **Safe background processing.** Heavy work runs through a single serial job
  queue, so only one transcription or LLM job uses the machine at a time.

## How it works

```
 RSS / Apple / YouTube
        │
        ▼
 ┌─────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
 │  Download   │──▶│  Transcribe  │──▶│    Digest    │──▶│    Export    │
 │ feedparser, │   │ Parakeet MLX │   │ clean + LLM  │   │ MD/JSON/HTML │
 │yt-dlp+ffmpeg│   │  / Whisper   │   │  summarize   │   │ blog/LinkedIn│
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

Every step is also all or nothing. If transcription fails part-way, or the LLM
cannot be reached or returns something unusable, nothing is stored, the
episode keeps its status, and the job fails with the reason. Failed jobs can
be retried from the dashboard, and the daily run picks up unprocessed
episodes again the next day, except one that failed three times in a row
(a broken audio file fails the same way every time), which waits until it
is run by hand. The one exception is the French translation of
a digest: if it fails, the English digest is kept and the translation can be
redone from the Admin page.

## Requirements

| Requirement | Notes |
|---|---|
| macOS on Apple Silicon (M1 or later), Linux or Windows | Apple Silicon gets Parakeet MLX and Whisper MLX; Linux and Windows get faster-whisper. An Intel Mac has no speech engine. |
| Python 3.11+ | |
| [ffmpeg](https://ffmpeg.org) | Audio normalization and chunking: `brew install ffmpeg` |
| [Ollama](https://ollama.com) | Default local LLM backend. Optional if you use OpenAI or Gemini. |
| [Deno](https://deno.com) | Only for YouTube sources: yt-dlp's default JavaScript runtime for YouTube's player challenges. `brew install deno` |
| Node.js 20.19+ and npm | Only needed to build or develop the web frontend |

Whisper and Parakeet models are downloaded automatically on first use (a few
hundred MB each). Plan for several GB of free memory while a model is loaded.

YouTube changes often break older yt-dlp releases. If YouTube downloads start
failing, upgrade it first: `pip install -U "yt-dlp[default]"`.

## Install on a Mac (one command)

On a Mac with Apple Silicon (M1 or newer), open Terminal and paste:

```bash
curl -fsSL https://raw.githubusercontent.com/MtlPhil/parakeet-podcast-processor/main/install.sh | bash
```

It needs no Homebrew, Xcode or git: Python (with the package versions pinned
in `uv.lock`), ffmpeg and deno are downloaded into `~/Parakeet`, and P3 starts in the background (and at every login). Your
browser then opens P3's setup: create your account (the first one is the
admin), then choose a language, the service that writes summaries, the daily
digest email, whether other devices on your home network can open P3, and
your first podcasts. Every page can be skipped and reopened from Settings.
Admins add other people, as admins or users, on the Admin page.

Run the same command again to update; your podcasts, accounts and settings
are kept. To remove P3, run `~/Parakeet/bin/p3-uninstall`.

This mode uses P3's own sign-in (`P3_AUTH=local`) and is for a Mac on a home
network, without a reverse proxy. For a server behind Traefik + Authelia,
use the manual installation below and set `P3_AUTH=proxy`.

## Installation

```bash
# 1. System tools
brew install ffmpeg
brew install ollama          # or download the app from https://ollama.com
ollama pull gemma4:12b       # any chat model works; set it in config
ollama pull bge-m3           # Ask's embedding model (optional)

# 2. Python package, at the dependency versions pinned in uv.lock
git clone https://github.com/MtlPhil/parakeet-podcast-processor.git
cd parakeet-podcast-processor
pip install uv               # or: brew install uv
uv sync --frozen --extra web # creates .venv; leave out --extra web for the CLI only
source .venv/bin/activate

# 3. Initialize directories, config and the database
p3 init
```

Add `--extra ask` for Ask's reranker (it pulls in PyTorch). Without it, Ask
still works, ranking passages by keyword and embedding only.

Without uv, `python3 -m venv .venv && source .venv/bin/activate &&
pip install -e ".[web]"` installs the newest versions that satisfy
`pyproject.toml` instead of the pinned ones.

`p3 init` checks for `ffmpeg`, `ollama` and `deno` and creates `data/`, `exports/`,
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

Shared settings live in `config/feeds.yaml`. The file is gitignored, so your
feed list stays local. Admins edit the same settings on the web app's
**Settings** page; each user's own settings (language, writing tone, writing
LLM, episodes per feed, daily run) are stored in the database and edited on
the same page.

The library in the database is the one source list. Feeds in the `feeds` list
are imported into it once, for the owner (the first admin), by `p3 fetch` or
the owner's daily run, and are then fetched like any other podcast. Removing a
podcast in the web app keeps it removed even if it is still in the file.

```yaml
feeds:
  - name: "Lenny's Podcast"
    url: "https://api.substack.com/feed/podcast/10845.rss"
    category: "product"
  # YouTube: a channel (any URL form: @handle, /channel/UC..., /c/, /user/),
  # a single video, or a playlist (each video becomes its own source).
  - name: "Some Channel"
    url: "https://www.youtube.com/@somechannel"
    category: "ai"

settings:
  max_episodes_per_feed: 3          # also caps each YouTube channel fetch
  audio_format: wav                 # wav or mp3

  parakeet_enabled: true            # false → Whisper for every language
  parakeet_model: "mlx-community/parakeet-tdt-0.6b-v3"
  parakeet_languages: [en]          # languages Parakeet handles; others → Whisper
  whisper_model: turbo              # turbo | large | medium | small | base | tiny,
                                    # or a Hugging Face repo of an MLX Whisper model
  release_ollama_before_transcription: true  # unload Ollama before transcribing

  llm_provider: ollama              # ollama | openai | gemini | anthropic
  llm_model: "gemma4:12b"           # model for ollama/openai (and anthropic, if a Claude model)
  ollama_base_url: "http://localhost:11434"
  gemini_model: "gemini-3.8-flash"  # model used when the provider is gemini

  export_format: [markdown, json]   # defaults for `p3 export`

  embedding_model: bge-m3           # Ask's Ollama embedding model; changing it re-indexes
  ask_rerank: true                  # needs the "ask" extra
  ask_min_free_memory: 25           # % free before Ask loads a local answer model
```

### LLM providers

| Provider | Credentials | Notes |
|---|---|---|
| `ollama` | none | Runs locally. Models are unloaded shortly after use to free memory. |
| `openai` | `OPENAI_API_KEY` environment variable, or the key alone in `config/openai_api_key.txt` (gitignored) | Any chat-completions model, e.g. `gpt-4o-mini`. |
| `gemini` | `GEMINI_API_KEY` environment variable, or the key alone in `config/gemini_api_key.txt` (gitignored) | Uses `gemini_model`. |
| `anthropic` | `ANTHROPIC_API_KEY` environment variable, or the key alone in `config/anthropic_api_key.txt` (gitignored) | Uses `llm_model` if it names a Claude model, else `claude-haiku-5-5`. |

The `openai` provider also talks to any server that speaks OpenAI's API
(LM Studio, llama.cpp, vLLM): set the `OPENAI_BASE_URL` environment variable
to its address, for example `http://localhost:1234/v1`. Such a server needs
no key.

In the web app, the synopsis, blog and LinkedIn actions can also switch to
Gemini for a single request without changing the default.

Gemini requests that hit an overload, a timeout or a dropped connection are
retried after 5, 15 and 45 seconds. If the model stays unavailable, P³
switches to `gemini-3.6-flash`, and every request for the next 30 minutes
goes straight to it rather than waiting out the retries again.

If a hosted provider is selected but no key is found, digests fail with a
message naming the missing key.

Admins can also enter the keys in the web app's setup wizard, which writes
those files.

### Emailed reports

The daily run can email each user its report. The mail server is a shared
setting; each user turns the email on and sets their address on the Settings
page.

```yaml
settings:
  smtp_host: "smtp.gmail.com"
  smtp_port: 587                    # 587 uses STARTTLS, 465 implicit TLS
  smtp_user: "you@example.com"
  smtp_from: "you@example.com"
```

The password is read from the `SMTP_PASSWORD` environment variable or
`config/smtp_password.txt` (gitignored). A report that fails to send is tried
again every 15 minutes: until it goes out, if the mail server couldn't be
reached (no network), for up to a day; eight times if the server refused it
(a wrong password, say).

## Using the CLI

Global options: `--config PATH`, `--db PATH`, `-v` (debug logging), `-q`
(warnings only).

| Command | Description |
|---|---|
| `p3 init` | Create directories, config and database; check prerequisites |
| `p3 fetch [--max-episodes N] [--dry-run]` | Import new feeds from `config/feeds.yaml`, then download new episodes for every podcast in the library (each user's, for that user) |
| `p3 transcribe [--episode-id ID] [--model NAME]` | Transcribe all `downloaded` episodes, or one |
| `p3 digest [--episode-id ID] [--provider P] [--model M]` | Summarize all `transcribed` episodes, or one |
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

The CLI and the web server cannot use the database at the same time. DuckDB
locks the file to one process, so while `uvicorn` is running every `p3`
command fails with "Could not set lock on file". Stop the server first, or do
the same work from the web app.

## Using the web app

For development, run the API and the Vite dev server side by side:

```bash
uvicorn p3.api.main:app --reload   # API on http://127.0.0.1:8000
cd frontend && npm run dev         # UI on http://localhost:5173 (proxies /api)
```

In the default sign-in mode, single, every request from this computer acts
as the library's owner. To act as a user of your choice, as a reverse proxy
would pass them, start the API with `P3_AUTH=proxy P3_DEV_USER=me` (see
[Users and sign-in](#users-and-sign-in)). The first user ever is an admin
and takes over everything already in the library.

The app's pages:

- **Dashboard**: pipeline counts, one-click "transcribe / digest / run
  everything" for the whole library, and a paginated job history. Failed
  jobs can be retried: fetch, transcribe, digest and pipeline jobs, and
  LinkedIn posts, synopses and blog posts, with the same choices (topic,
  date, model) they were started with.
- **Podcasts**: add a feed (RSS, Apple Podcasts link, or YouTube channel,
  video or playlist), fetch new episodes,
  run steps for every episode of a podcast, and download all transcripts as a
  zip.
- **Episode**: run each step, read the transcript and summary, generate the
  long-form synopsis, and download the transcript.
- **Content**: generate LinkedIn posts (pick a podcast, then an episode) or
  themed blog posts from a day's digests. Browse everything generated so far.
- **Search**: find episodes by words in their title, summary or transcript,
  with the matching transcript lines and timestamps.
- **Ask**: a question in plain language, answered from your episodes with
  numbered citations that open the transcript at that moment. Filter by show
  and date, follow up in the same conversation, rate answers, copy them to
  your notes. Answers use your writing LLM. While a transcription runs, Ask
  loads no model and answers with keyword search only; a local answer model
  is only loaded with enough memory free, otherwise Ask shows the passages
  it found. When the model is slow to start, the page says why it waits; an
  answer is saved even if you leave the page, and appears when you reopen
  the conversation.
- **Settings**: your language, writing tone (sent to the writer with every
  post), writing LLM, episodes per feed and daily run. Admins also see the
  shared transcription and summarizing settings.
- **Admin** (admins only): users and their roles; free disk space; translate
  digests made before French versions existed; build the Ask index for
  episodes digested before it existed; Ask's evaluation and rated answers.

Interactive API docs are available at `http://127.0.0.1:8000/docs`.

## Deployment

P³ runs on your own Mac. In production, one `uvicorn` process serves both the
API and the built frontend.

```bash
cd frontend && npm run build && cd ..
uvicorn p3.api.main:app --host 127.0.0.1 --port 8000
```

Run it from the repository root. Data, config and output paths are relative to
the working directory.

### Desktop app

P³ in a window of its own, with no browser, no sign-in and no background
service:

```bash
uv sync --frozen --extra desktop     # or: pip install -e ".[desktop]"
p3-app
```

`p3-app` starts the server inside the app, reachable from this computer
only, and opens a native window on it (WebKit on macOS, WebView2 on Windows,
GTK or Qt on Linux). Closing the window stops it, so nothing runs, including
the daily run, while the app is closed. The library lives in your
application data folder (`~/Library/Application Support/P3` on macOS,
`%LOCALAPPDATA%\P3` on Windows, `~/.local/share/P3` on Linux); set `P3_HOME`
to use another, such as an existing `~/P3`. Only one P³ can have a library
open: stop the background service before opening the same library in the app.

This is the launcher only. There is no installer or signed download yet: it
needs Python and the steps above.

### On a phone

The web app can be added to a phone's home screen (Share → Add to Home
Screen on iOS, Install app on Android) and then opens full-screen like an
app. It still talks to the P³ server on your computer, so the phone must be
able to reach it.

### Container

For Linux, or anywhere Docker runs:

```bash
docker build -t p3 .
docker run -d -p 8000:8000 -v p3-library:/library p3
```

Open <http://localhost:8000>; the first visit creates the admin account. The
`/library` volume holds everything: database, audio, settings, generated
files and the downloaded speech models. To use a local Ollama, set its URL
in Settings to one the container can reach (on Docker Desktop,
`http://host.docker.internal:11434`). For an NVIDIA GPU, add `--gpus all`;
the image carries no CUDA libraries of its own, so this needs a base image
that does and is untested.

### Users and sign-in

The `P3_AUTH` environment variable picks one of three modes. Unset, it is
single.

**`P3_AUTH=single` (the default): one person, no sign-in.** For P³ running on
your own computer for you alone. Every request acts as the library's owner
(created as `me` on first use, or the existing first admin). Nothing is
asked, so only requests from the same computer are answered; anything else
gets a 403. So does a request that came through a proxy (one carrying a
`Forwarded`, `X-Forwarded-For`, `X-Real-IP` or `Remote-User` header): a proxy
on the same computer would otherwise make every visitor the owner.

**`P3_AUTH=local`: P³'s own accounts.** The mode the Mac installer sets up,
for a machine on a home network. The setup wizard creates the first account,
an admin; admins create the others on the Admin page. Passwords are stored
as scrypt hashes and a sign-in lasts 30 days. The `Remote-User` header and
`P3_DEV_USER` are ignored in this mode.

**`P3_AUTH=proxy`: Authelia.** P³ handles no passwords. It expects
a reverse proxy in front of it that signs users in with
[Authelia](https://www.authelia.com/)'s forward-auth and passes the user in
the `Remote-User` header (plus `Remote-Name` and `Remote-Email`). Requests
without that header are refused.

- **The header is trusted.** Keep P³ bound to `127.0.0.1` (or a network only
  the proxy can reach) so nobody can send the header themselves, and make the
  proxy overwrite any copy a client sends; Traefik's `authResponseHeaders`
  does.

> **Upgrading a proxy server.** Proxy mode used to be the default. A server
> behind Authelia that never set `P3_AUTH` must now set `P3_AUTH=proxy`
> (under `EnvironmentVariables` in its launchd plist, say). Until it does,
> it refuses every request that comes through the proxy with a 403; it lets
> nobody in by mistake.

In every mode:

- **Roles.** The first user is an admin and takes over everything already in
  the library. Admins see the shared settings and the Admin page, where they
  can make other users admins and delete users. Users can delete their own
  account.
- **Separate libraries, shared processing.** Each user sees only the podcasts
  they follow and the episodes they have. Deleting an episode removes it from
  that user's library only; their fetches won't bring it back unless they
  pick it again. A podcast's audio and data are deleted once nobody follows
  it.

A Traefik example in its file-provider format (adapt the hostname and the
Authelia address to your setup). P³ runs on the host, not in a container, so
the service points at `host.docker.internal` when Traefik runs in Docker:

```yaml
http:
  middlewares:
    authelia:
      forwardAuth:
        address: "http://authelia:9091/api/authz/forward-auth"
        trustForwardHeader: true
        authResponseHeaders: ["Remote-User", "Remote-Name", "Remote-Email", "Remote-Groups"]
  routers:
    p3:
      rule: "Host(`p3.example.com`)"
      middlewares: ["authelia"]
      service: p3
  services:
    p3:
      loadBalancer:
        servers:
          - url: "http://host.docker.internal:8000"
```

For local development in proxy mode without a proxy, set `P3_AUTH=proxy` and
`P3_DEV_USER` to the username to act as; it is used only when the header is
absent. Never set it on a server others can reach.

### Environment variables

| Variable | Effect |
|---|---|
| `P3_HOME` | The folder holding `data/`, `config/` and the generated files. Unset, it is the folder P³ is started from |
| `P3_AUTH` | `single` for one person with no sign-in, `local` for P³'s own accounts, `proxy` for a reverse proxy with Authelia; anything else, or unset, is `single` |
| `P3_DEV_USER` | Proxy mode only: the user to act as when no `Remote-User` header arrives |
| `P3_LOG_LEVEL` | Log level of the web server's own messages (default `INFO`) |
| `OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `SMTP_PASSWORD` | Secrets; each takes precedence over its file in `config/` |
| `OPENAI_BASE_URL` | Another server for the `openai` provider (default: OpenAI's own) |

`P3_HOST`, `P3_PORT` and `P3_MANAGED` belong to the Mac installer's launcher
(`bin/p3-serve`), and the other `P3_*` variables to `install.sh`, which
documents them at its top.

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
hosted provider. Behind a reverse proxy, add `P3_AUTH` there too, set to
`proxy`; without it the server answers only this computer, as one person.

### Exposing the dev server on a LAN

The Vite dev server listens on `localhost` only by default. To reach it from
another device, create `frontend/.env.local` (gitignored):

```bash
P3_DEV_HOST=0.0.0.0
P3_ALLOWED_HOSTS=p3.example.com   # optional, comma-separated extra hostnames
```

Only do this on a network you trust: the API sees the dev server's requests
as coming from this computer, so in single mode (or proxy mode with
`P3_DEV_USER` set) everyone who reaches it acts as that one user.

### Scheduling the CLI

Without the web app, the pipeline can run on a schedule with cron or launchd.
This only works while the web server is stopped (see the database lock above).
The web app's per-user daily run (Settings) is usually the better option:

```cron
0 6 * * * cd /path/to/parakeet-podcast-processor && .venv/bin/p3 -q fetch && .venv/bin/p3 -q transcribe && .venv/bin/p3 -q digest && .venv/bin/p3 -q export
```

## Architecture

```
p3/
├── cli.py            Click command group (`p3 …`)
├── downloader.py     RSS and YouTube fetching, download with retry, ffmpeg normalization
├── youtube.py        YouTube URL parsing, listing and audio download via yt-dlp
├── url_resolver.py   Source URL resolution: RSS, Apple Podcasts (iTunes Lookup API), YouTube
├── transcriber.py    Language detection, Parakeet MLX (chunked), Whisper MLX;
│                     faster-whisper where MLX doesn't run
├── cleaner.py        Digests, long-form synopsis, Quebec French translation
├── writer.py         Graded blog posts, LinkedIn posts, social snippets
├── exporter.py       Markdown / JSON / HTML formatting
├── mailer.py         The daily run's emailed report (SMTP)
├── llm.py            Ollama / OpenAI / Gemini / Anthropic chat client, provider resolution
├── chunker.py        Ask: an episode cut into ~60 s passages, plus notes and summary
├── embedder.py       Ask: bge-m3 embeddings through Ollama, the cross-encoder reranker
├── indexer.py        Ask: passages, embeddings and BM25 terms stored after each digest
├── retriever.py      Ask: BM25 + vector search, fusion, rerank, per-episode cap
├── ask.py            Ask: question step, retrieval, streamed answer, citation checks
├── model_gate.py     Ask: no model calls while transcription models are loaded
├── eval_ask.py       Ask: retrieval and answer evaluation
├── desktop.py        The app in its own window (`p3-app`)
├── paths.py          Where every file lives: under P3_HOME, or the current folder
├── secrets.py        API keys and the mail password: environment, else a file
├── settings.py       The shared settings and their defaults
├── database.py       The storage layer, assembled from db/
├── db/               DuckDB storage by subject: connection, schema (numbered
│                     migrations), library, digests, jobs, users, search, ask
└── api/
    ├── main.py       FastAPI app, lifespan, cross-site write check, static frontend
    ├── auth.py       Who is making a request: an account, the proxy's header, or the owner
    ├── deps.py       Shared database instance, config I/O, per-user settings
    ├── job_queue.py  Single-threaded serial job runner with a priority lane
    ├── tasks/        Job wrappers: pipeline, content and Ask jobs; the daily run's steps
    ├── scheduler.py  Starts each user's daily run when its time comes
    ├── content.py    Reading generated posts back from disk
    ├── models.py     Pydantic request/response schemas
    └── routers/      podcasts, episodes, jobs, transcripts, summaries, exports,
                      blogs, linkedin, search, ask, settings, daily_run,
                      users, auth, setup
frontend/             React + Vite + Tailwind single-page app
tests/                pytest suite (no external services required)
bin/                  Launcher and uninstaller used by the Mac installer
install.sh            One-command Mac installer and updater
docs/                 Plans and design decisions
```

### Data model

DuckDB file at `data/p3.duckdb`:

- `podcasts`: sources, shared by everyone who follows them. Title, canonical
  URL in `rss_url` (unique), category, and `source_type`: `rss`,
  `youtube_channel` or `youtube_video`
- `episodes`: title, publish date, audio or YouTube watch URL (unique), the
  feed's `guid` (which stays when a feed moves an episode to a new audio URL,
  so it isn't downloaded again), local file path, status
- `transcripts`: one row per segment with start/end timestamps and text
- `summaries`: short summary, long-form synopsis, and JSON lists of topics,
  themes, quotes and companies, keyed by episode and digest date, plus Quebec
  French versions (`*_fr`) of the summary, synopsis, topics and themes
- `jobs`: background job type, status, progress, message, error, timestamps,
  the user who started it, the daily run it belongs to, and what it was asked
  to do (a fetch's episodes, a writing job's topic, date and model) so it can
  be retried as it was
- `users`: username, name, email, role (`admin` or `user`), own settings, and
  in local sign-in mode a password hash
- `sessions`: local sign-in sessions, keyed by the hash of their cookie token
- `user_podcasts`, `user_episodes`, `user_deleted_episodes`: who follows which
  podcast, who has which episode, and which episodes a user deleted
  (remembered by URL and guid, so a deleted episode's data can go)
- `chunks`, `chunk_terms`: Ask's passages with their embeddings, and their
  BM25 terms
- `ask_threads`, `ask_messages`: Ask conversations; each answer keeps the
  passages and scores that produced it, its timings and its model versions
- `app_state`: small key-value store (daily run dates, imported config feeds)
- `schema_migrations`: which numbered migrations this database has had

Rows are returned as dicts keyed by column name. Schema changes are numbered
migrations (`p3/db/schema.py`): each runs once per database, in order, when
it is opened. A database last opened by a newer P³ is refused. Writes that belong together (a transcript with its
episode's new status, a replaced summary) run in one transaction. Deleting a
podcast with its episodes does not: DuckDB refuses to delete a row and the
rows referencing it in the same transaction.

### Background jobs

API endpoints never do heavy work inline. They create a `jobs` row and enqueue
a task on a single worker thread (`job_queue.py`). Tasks run strictly one at a
time, because loading several ML models at once can exhaust memory on a laptop.
The queue has two lanes: a daily run's jobs go ahead of everything else, and
the backlog and work started from the web app wait until it is done.
The frontend polls `/api/jobs/{id}` for progress. Jobs left pending or running
by a previous process are marked as failed on startup, and they can be retried.

### LLM usage

`cleaner.py`, `writer.py` and `ask.py` share one `LLMClient` (`llm.py`).
Requests are bounded in two ways:

- **Input.** Transcripts are cut to about 100k characters, keeping the start and
  end. The synopsis works section by section rather than on the whole
  transcript, because small local models tend to compress long inputs heavily.
- **Output.** Every request sets a token cap, and Ollama gets a mild repeat
  penalty, so a model caught in a repetition loop cannot run away.

Structured JSON is read from model output starting at its first `{`, so a
preamble or code fences around it don't matter; a reply with no JSON object is
asked for once more, then fails the digest. List fields are coerced to plain
strings.

### Output locations

| Output | Location |
|---|---|
| Normalized audio | `data/audio/` |
| Database | `data/p3.duckdb` |
| Digests and transcript exports | `exports/<user>/` (CLI: `exports/`) |
| Blog posts, English + Quebec French (Markdown + YAML front matter) | `blog_posts/<user>/` (CLI: `blog_posts/`) |
| LinkedIn posts (English + Quebec French) | `linkedin_posts/<user>/` |

Files at the top of these folders, from before users existed, belong to the
owner.

## Development

```bash
uv sync --frozen --extra dev --extra web   # the versions CI uses, into .venv
uv run pytest tests/ -v              # full suite, no network or models needed
uv run black . && uv run isort .     # formatting
uv run mypy p3/                      # type checking
cd frontend && npm run lint          # frontend lint
```

After changing dependencies in `pyproject.toml`, run `uv lock` and commit
`uv.lock`.

GitHub Actions runs all of these on every push to `main` and on pull requests
(`.github/workflows/ci.yml`): formatting checks, mypy and the test suite on
both an Apple Silicon macOS runner and Linux, and ESLint plus a production
build for the frontend.

Tests cover the database layer, API endpoints (through `TestClient` against a
temporary database), the job queue and task wrappers, text-processing helpers
in the cleaner and writer, the exporters, the LLM client's configuration logic,
and the RSS and YouTube download paths with the network mocked out. The speech
engines and the LLM are replaced by stand-ins: model inference, real LLM
requests, the CLI and the Mac installer are not exercised.

See [ROADMAP.md](ROADMAP.md) for planned work.

## License

P³ is released under the [MIT License](LICENSE), the license of the project it
was forked from ([haasonsaas/parakeet-podcast-processor](https://github.com/haasonsaas/parakeet-podcast-processor)).

The speech and language models P³ downloads at run time are not part of it and
carry their own licenses.

## Acknowledgements

- The workflow, and in particular the graded writing loop, is inspired by
  [Tomasz Tunguz](https://tomtunguz.com)'s description of his podcast
  processing system on the *How I AI* podcast.
- Transcription by [NVIDIA Parakeet](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3)
  via [parakeet-mlx](https://github.com/senstella/parakeet-mlx) and
  [OpenAI Whisper](https://github.com/openai/whisper).
- Originally forked from
  [haasonsaas/parakeet-podcast-processor](https://github.com/haasonsaas/parakeet-podcast-processor).
- The web app (FastAPI backend and React frontend), the test suite and several
  module rewrites come from the
  [kmbuilds-dbd/parakeet-podcast-processor](https://github.com/kmbuilds-dbd/parakeet-podcast-processor)
  fork.
