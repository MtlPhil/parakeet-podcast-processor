# Roadmap

What is planned and not built yet. The first section is the agreed direction,
in order; the rest is a backlog grouped by area, not ordered by priority.

## Direction

The goal is a P³ that people other than its author can install and use,
on more than Apple Silicon, with a desktop app in place of a browser tab.
It stays a Python project: what ties it to the Mac is two speech libraries
and the installer, not the language.

1. **Restructure, with no change in behaviour.** Done: one home folder for
   every file (`P3_HOME`), one place that reads secrets, one default per
   shared setting, the database layer split by subject with numbered
   migrations, the job wrappers split into pipeline, content, Ask and
   daily-run modules, and a single-user mode with no sign-in
   (`P3_AUTH=single`), now the default. Still to do:
   - Secrets in the OS keychain rather than files in `config/`.
2. **Transcription beyond Apple Silicon.** Done: faster-whisper transcribes
   on Linux and Windows, and there is a container image. Still to do:
   - Run it on real episodes and time it, CPU and GPU; pick the default
     model per machine from the result (it is `turbo` everywhere today).
   - A faster engine for English off the Mac, to match Parakeet there
     (Parakeet through ONNX is the candidate to measure).
   - A GPU image, published images, and tagged releases.
3. **A wider LLM layer, and Ask.** Done: an Anthropic provider, any
   OpenAI-compatible server through `OPENAI_BASE_URL`, and Ask: questions
   answered from the library with cited passages (keyword and meaning
   search, reranking, streamed answers, conversations, evaluation), as
   designed in [docs/ask-plan.md](docs/ask-plan.md). Still to do:
   - The web as a second source: a hosted model's built-in search where
     there is one, a search API for local models, with web and podcast
     citations kept apart.
   - Tool calls for every provider (every one of them streams).
   - Send real requests through the Anthropic provider and OpenAI's own API,
     streamed and whole (the README's "LLM providers" shows how). Both are
     tested only with stand-ins; an OpenAI-compatible server (Ollama's)
     answered real ones.
   - Speakers: done (diarization, names, citations that say who spoke).
     Still to do: "what did X say" as a filter; editing a name on the episode page; remembering a show's
     host voice across episodes; speakers for episodes digested before,
     whose audio is gone.
   - Ask, from [docs/ask-plan.md](docs/ask-plan.md):
     - Decide local or hosted answers from the measurements in the plan, or
       try a smaller local answer model, then check the attribution rules
       on a broad question.
     - Count by concept, not only by the words the question names.
     - Measure subtler overstatement than the support check catches
       (`python -m p3.eval_ask answers`).
     - Tools for the model, beyond the broad-question routing.
     - Citation popovers that work on touch screens; flag a single
       citation.
     - An answer cut short by a server restart is lost (answers run on
       daemon threads).
4. **A desktop app.** Done: `p3-app` opens P³ in a native window with the
   server inside it, and the web app can be installed on a phone's home
   screen. Still to do:
   - Installers. Today the app needs Python and a checkout. A double-click
     download per OS means bundling Python, the speech models' libraries and
     ffmpeg (PyInstaller or Briefcase), then signing and notarizing, which
     needs an Apple Developer ID and a Windows certificate.
   - The daily run while the window is closed (a tray icon, or a login
     item).
   - Opening an existing `~/P3` library from the app without setting
     `P3_HOME` by hand, and handing over from the background service.
   - Open `p3-app` on macOS, Windows and Linux once: the launcher is tested,
     but no window has been opened on any of them.
   - Install the web app on a phone's home screen once; it is untried.
   - A native iPhone app ([docs/ios-app-plan.md](docs/ios-app-plan.md)),
     deferred until after.

## Web app

- **Push-based job progress.** Replace polling of `/api/jobs/{id}` with
  Server-Sent Events or WebSockets.
- **Ranked search.** The Search page is a case-insensitive substring match
  over titles, summaries and transcripts. Ask's BM25 index (`chunk_terms`)
  could rank it.
- **Podcast discovery.** Browse and search a podcast directory by category
  instead of pasting feed URLs.
- **Blog post tab on the episode page.** Show posts generated from an episode,
  with their grading history.
- **Frontend tests.** No JavaScript test framework is set up yet.

## Content and analysis

- **Company extraction for CRM.** Push the "companies mentioned" list to a CRM
  or spreadsheet.
- **Investment-thesis generation.** Turn recurring themes across episodes into
  structured investment or research theses.
- **The summary's format.** The summary prompt asks for no headings, which
  the local model often ignores; every display (episode page, emails, HTML
  export) now shows its titles and lists. Either ask for that format in the
  prompt, or keep tolerating both.

## Testing

- Integration coverage for the CLI commands, transcription backends and LLM
  request paths, using recorded fixtures rather than live services.
- An end-to-end smoke test of fetch → transcribe → digest → export on a short
  sample feed.
- A CI job that runs `install.sh` on a macOS runner.
