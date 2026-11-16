# Roadmap

What is planned and not built yet. The first section says where the work
stands and in what order it continues; then the agreed direction, in order;
the rest is a backlog grouped by area, not ordered by priority.

## Where things stand (2026-11-15)

Working through the rest of this file in this order, one pull request per
item, docs updated with each:

1. Done: Ask ranks sponsor reads and jingles last and skips cold opens;
   "what did X say" searches only X's passages; admins rename a speaker on
   the episode page; the Search page is ranked by Ask's keyword index; job
   progress is pushed to the page; the episode page has a Posts tab; Add
   Podcast can search and browse Apple's directory; Ask's citations open a
   popover on tap, where one can be flagged; Ask can also search the web;
   an answer a restart cut short is written again; counting questions also
   list episodes that discuss it in other words; the answer evaluation
   judges each cited claim (supported, overstated, misattributed,
   unsupported); broad questions get a research step in which a hosted
   model calls read-only tools (search again, list episodes, read
   summaries, count mentions) before the answer, on tool calls for every
   provider; a show's hosts are recognized by voice in its later episodes;
   the summary prompt asks for a set format (bold section titles, short
   paragraphs or lists, Takeaways last); an end-to-end smoke test runs
   `p3 fetch`, `transcribe`, `digest` and `export` on a sample feed; CI
   runs `install.sh` on a macOS runner (install, update, uninstall); a
   manual fetch goes on through the whole chain; a step-by-step guide to
   trying P³ on Linux or Windows, with the container finding Ollama on its
   host; the digest, `p3 write`, LinkedIn posts, Ask and Gemini's requests
   are tested on recordings of real models, and the transcription backends
   on what the real speech engines returned (which found av 19 breaking
   faster-whisper, now kept below it); the frontend has tests (Vitest:
   `MarkdownText`, the Podcasts sort, tags, the page language, the API
   client), run by CI, and its shared components and the job follower are
   tested in a DOM (jsdom and Testing Library); the desktop app is on hold;
   Ask's evaluation can compare answers with and without the research
   step, on broad questions it generates from the companies several
   episodes name; run on the library with Gemini 3.8 Flash, research
   cites about three times as many of the relevant episodes; the judge
   checks a sentence against all its passages together; the answer prompt
   keeps quotation marks for exact words; sponsor reads (passages said
   again in another episode of the same show) are left out of Ask and of
   the digest.
2. Next: try Ollama's tool calling against the same comparison. Nothing
   else is unblocked.

On hold: secrets in the keychain and timing transcription on Linux and
Windows (no machine to test on); real Anthropic and OpenAI requests
(waiting on someone with a key); downloading the library again so older
episodes get speakers; the desktop app (decided 2026-11-15: until there is
a signed installer it gives nothing over the web app installed from the
browser, which keeps the server running for the phone and the daily run).

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
   - The web as a second source: done (Google Search through Gemini, or
     SearXNG; [W1] citations listed apart). Still to do: one real grounded
     answer (the Gemini key tried has no grounding quota), Anthropic's and
     OpenAI's own web search tools.
   - Tool calls for every provider: done (`LLMClient.chat_tools`, used by
     Ask's research step). Tried for real with Gemini only; Anthropic's,
     OpenAI's and Ollama's are tested with stand-ins.
   - Send real requests through the Anthropic provider and OpenAI's own API,
     streamed and whole (the README's "LLM providers" shows how). Both are
     tested only with stand-ins; an OpenAI-compatible server (Ollama's)
     answered real ones.
   - Speakers: done (diarization, names, citations that say who spoke, a
     show's hosts recognized by voice across episodes). Still to do:
     speakers for episodes digested before, whose audio is gone.
   - Ask, from [docs/ask-plan.md](docs/ask-plan.md):
     - Check the attribution rules on a real broad question, answered by
       Gemini (decided 2026-11-14: answers by the writing LLM, Gemini on this
       Mac; search stays local).
     - Run the claim judge on real answers once there are some
       (`python -m p3.eval_ask answers --stored 50 --judge-provider gemini`)
       and act on what it finds.
     - Tools for the model: done for broad questions with a hosted model,
       and the evaluation compares answers with and without them
       (`generate --broad`, `answers --stratum broad --provider gemini
       [--no-research]`, `compare`). Left: run it, and try Ollama's tool
       calling against it before giving local models the tools.
4. **A desktop app.** Done: `p3-app` opens P³ in a native window with the
   server inside it, and the web app can be installed on a phone's home
   screen or a computer's Dock. On hold (2026-11-15): without a signed
   installer the app still needs Python and a checkout, and someone with
   those gets the same window from the installer plus Add to Dock, with the
   server left running for their phone and the daily run. The installer
   comes first; the rest waits on it. Still to do:
   - Installers. Today the app needs Python and a checkout. A double-click
     download per OS means bundling Python, the speech models' libraries and
     ffmpeg (PyInstaller or Briefcase), then signing and notarizing, which
     needs an Apple Developer ID and a Windows certificate.
   - The daily run while the window is closed (a tray icon, or a login
     item).
   - Opening an existing library (the installer's `~/Parakeet`) from the
     app without setting `P3_HOME` by hand. If the background service has
     it open, connect to that service rather than stop it: stopping it
     would cut off the phone while the window is open.
   - Open `p3-app` on macOS, Windows and Linux once: the launcher is tested,
     but no window has been opened on any of them.
   - Install the web app on a phone's home screen and a computer's Dock
     once; both are untried.
   - A native iPhone app ([docs/ios-app-plan.md](docs/ios-app-plan.md)),
     deferred until after.

## Web app

- **Frontend tests.** Done: Vitest in a Node environment, on `MarkdownText`
  (rendered to static markup) and the logic outside components (the
  Podcasts sort, tags, `languageOf`, the API client with `fetch` stubbed).
  In a DOM (jsdom and Testing Library, per file): the account-deletion
  confirmation, the episode picker, the tag field, the language toggle,
  job progress, renaming speakers, and `useJobPoller` (its stream, falling
  back to polling, a cleared job). Still to do: the pages themselves
  (Ask's streamed answers, Settings, Add Podcast's preview).

## Testing

- Integration coverage on recorded fixtures rather than live services.
  Done: the LLM and embedding request paths (Ollama and Gemini) and the
  CLI's `digest`, `write` and `export-transcript`
  (`tests/test_recorded.py`), and the transcription backends (mlx-whisper,
  Parakeet, faster-whisper) on their recorded output
  (`tests/test_recorded_speech.py`). Language detection itself is not
  replayed (it runs MLX on the audio).
