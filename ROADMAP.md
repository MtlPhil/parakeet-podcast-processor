# Roadmap

What is planned and not built yet. The first section says where the work
stands and in what order it continues; then the agreed direction, in order;
the rest is a backlog grouped by area, not ordered by priority.

## Where things stand (2026-11-14)

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
   `p3 fetch`, `transcribe`, `digest` and `export` on a sample feed.
2. Next, Testing: recorded-fixture integration tests, a CI job for
   `install.sh`.

On hold: secrets in the keychain and timing transcription on Linux and
Windows (no machine to test on); real Anthropic and OpenAI requests
(waiting on someone with a key); downloading the library again so older
episodes get speakers; the desktop app.

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
     - Tools for the model: done for broad questions with a hosted model.
       Left: run the evaluation with and without research, and try Ollama's
       tool calling against it before giving local models the tools.
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

- **Frontend tests.** No JavaScript test framework is set up yet.

## Testing

- Integration coverage for the CLI commands, transcription backends and LLM
  request paths, using recorded fixtures rather than live services.
- A CI job that runs `install.sh` on a macOS runner.
