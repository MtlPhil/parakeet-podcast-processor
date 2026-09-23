# Roadmap

Planned and proposed work that is not implemented yet. Items are grouped by
area, not ordered by priority.

## Web app

- **Push-based job progress.** Replace polling of `/api/jobs/{id}` with
  Server-Sent Events or WebSockets.
- **Full-text search.** Search across transcripts, summaries and generated
  posts. This becomes essential once a library has more than a few dozen
  episodes.
- **Authentication.** Token or session auth for the API and a login page, so
  the app can be exposed beyond `localhost` without a separate proxy.
- **Multi-user support.** Per-user data isolation on top of authentication.
- **Podcast discovery.** Browse and search a podcast directory by category
  instead of pasting feed URLs.
- **Blog post tab on the episode page.** Show posts generated from an episode,
  with their grading history.
- **Frontend tests.** No JavaScript test framework is set up yet.

## Pipeline

- **Anthropic LLM provider.** Add it alongside Ollama, OpenAI and
  Gemini in `p3/llm.py`.
- **Automatic audio cleanup.** Delete normalized audio older than a configurable
  number of days once an episode is processed.
- **Email delivery.** Send the HTML digest (`p3 export --format html`) by email
  on a schedule.
- **Reuse the cleaned transcript.** Store the LLM-cleaned transcript from the
  digest step so the on-demand synopsis does not clean it again.
- **Retry for writing jobs.** Extend job retry to blog, LinkedIn and synopsis
  jobs.
- **Speaker diarization.** Fill the `speaker` field on transcript segments,
  which is currently always empty.

## Content and analysis

- **Company extraction for CRM.** Push the "companies mentioned" list to a CRM
  or spreadsheet.
- **Investment-thesis generation.** Turn recurring themes across episodes into
  structured investment or research theses.

## Code quality

- Bring the codebase to a clean `black`/`isort`/`mypy` baseline and enforce it
  in CI.
- Serve `index.html` for unknown non-API paths so deep links survive a page
  refresh in the production build.

## Testing

- Integration coverage for the CLI commands, RSS parsing and download,
  transcription backends and LLM request paths, using recorded fixtures rather
  than live services.
- An end-to-end smoke test of fetch → transcribe → digest → export on a short
  sample feed.
