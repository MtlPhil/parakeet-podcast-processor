# Ask: natural-language questions over the library

**Status: in progress on the `ask` branch (started 2026-10-28).** This document records the plan and the decisions made so far; [Progress](#progress) says what is built.

## Goal

An **Ask** page where a user types a question in English or French ("What have my podcasts said about the Bank of Canada this fall?") and gets an answer built only from their own library, with every claim cited to an episode and a transcript timestamp. Follow-up questions work. When the library doesn't contain the answer, it says so instead of guessing.

"Professional-grade" here means the retrieval and answer quality of a commercial product, measured rather than eyeballed, on the existing single-Mac, single-process setup. It does not mean multi-tenant scaling.

```
question ─▶ understand (rewrite + keywords EN/FR + filters)
         ─▶ retrieve: BM25 top 50 ∪ vector top 50, scoped to the user's library + filters
         ─▶ fuse (reciprocal rank fusion) ─▶ rerank top 50 (cross-encoder) ─▶ keep top ~12 above threshold
         ─▶ answer with [cN] citations, streamed ─▶ validate citations ─▶ links to episode + timestamp
```

## Decisions made

| Topic | Decision |
|---|---|
| Database | Stay on DuckDB. The API is the only process, so its lock isn't a problem, and a Postgres migration would buy nothing at this scale |
| Keyword search | BM25 computed in SQL over our own term table (`chunk_terms`), accents stripped, no stemmer. Not DuckDB's `fts` extension: it isn't bundled (it downloads on first use, which breaks offline tests) and its index must be rebuilt after every insert, while a term table updates per episode. The query step produces keywords in both English and French, which covers both languages without one index per language |
| Vector search | Embeddings in a `FLOAT[1024]` column, brute-force `array_cosine_similarity`. A personal library (~100k chunks) scans in well under a second; the `vss` HNSW index is only worth adding if Phase 0 measures otherwise |
| Embedding model | `bge-m3` through Ollama: multilingual, so a French question finds English passages and vice versa, 8k-token input, 1024 dimensions. Shared setting (`embedding_model`), since the corpus is embedded once for everyone |
| Reranker | `bge-reranker-v2-m3` cross-encoder (multilingual), through `sentence-transformers`, in a new optional `ask` extra with a guarded import (`RERANKER_AVAILABLE`). Without it, retrieval falls back to the fused order |
| Answer LLM | The user's writing LLM (`llm_provider` / `llm_model` / `gemini_model`), no new setting |
| Answer language | The question's language, whatever the user's setting (decided 2026-10-28) |
| Reranker dependency | `sentence-transformers` and PyTorch accepted as a dependency (decided 2026-10-28), still behind the optional `ask` extra |
| Chunks | Transcript segments grouped into ~60 s passages on segment boundaries, one segment of overlap, plus the episode's summary, topics and quotes as their own chunks. Each chunk is embedded with a prefix: podcast, episode title, date, and the episode's short summary |
| Scoping | Every retrieval query joins `user_episodes`, like `search`. Deleting an episode deletes its chunks |
| Deep links | Citations open Episode Detail scrolled to the cited segment (`/episodes/:id?t=<seconds>`). Audio is deleted after the digest, so there is no playback |
| Evaluation | A question set kept in gitignored `data/eval/` (it's derived from the library, which stays out of this public repo), run as an admin job. Pure helpers (chunking, fusion, citation parsing) get unit tests in CI as usual |

## Data model

- **`chunks`**: `id`, `episode_id`, `kind` (`transcript` | `summary` | `topics` | `quote`), `start`, `end` (seconds, transcript chunks only), `text`, `prefix`, `language`, `embedding FLOAT[1024]`, `embed_model`, `created_at`. `embed_model` lets a model change re-index in the background instead of mixing incomparable vectors.
- **`chunk_terms`** (`chunk_id`, `term`, `tf`) and each chunk's token count: the BM25 index, written with the chunk and deleted with it. Document frequencies and the average length come from the same tables at query time.
- **`ask_threads`** (`id`, `username`, `title`, `created_at`) and **`ask_messages`** (`thread_id`, `role`, `content`, `citations` JSON, `retrieval` JSON with chunk IDs and scores per stage, `timings` JSON, `models` JSON, `rating`).
- `purge_episode` adds `chunks` to the tables it clears.

## Professional additions (decided 2026-10-28)

On top of the phases below, in priority order:

1. **Speakers.** Diarize each transcript as its own queued job, then name the speakers with one LLM call per episode (show notes plus the self-introductions in the first minutes). Passages carry their speaker; citations read "*Name, 14:32*"; "what did X say" becomes a filter.
2. **Index what is thrown away today.** Store each episode's show notes (the feed description, read during the fetch but not kept) and index them. Build a per-episode vocabulary of names (show notes, the digest's companies) to match transcription misspellings in keyword search. A per-chunk entity index (people, companies, tickers) for structured filters.
3. **Beyond lookup questions.** Read-only tools for the model (`search_passages`, `list_episodes(filters)`, `get_summary`, `count_mentions`), at most ~5 steps under a time budget, so counting, comparison and overview questions work; overviews summarize each episode's summary and then combine them. Gemini first; Ollama keeps the fixed pipeline until its tool calling passes the evaluation.
4. **Statistically honest evaluation.** About 200 questions in strata (lookup, who-said-what, counting, comparison, overview, French, mixed-language, no answer), metrics per stratum, paired comparison with a confidence interval (paired bootstrap), the LLM judge checked against ~50 human ratings, and minimum scores per stratum that a prompt, model, chunking or index change must meet.
5. **Reproducibility.** Prompts in versioned files; every answer records prompt versions, models and index version. A new index is built beside the old one and switched to only after the evaluation passes. Per-stage latency targets (first token < 2 s, full answer < 10 s with Gemini), slow cases on the Admin page.
6. **Hardening.** Retrieved passages are quoted data, never instructions; tools are read-only and scoped to the user's library. When the LLM fails, show ranked passages instead of an error. Tests that try to reach another user's passages through every tool and filter.

**Revised order:** show notes and the evaluation set first, then Phases 1–4, then tools and question routing, then speakers (the biggest quality gain, but the heaviest load on the 16 GB machine).

## Overnight runs and the Gemini fallback (decided 2026-10-28)

Real-model runs during unattended work use local models only (`bge-m3`, the reranker, `gemma4:12b`), on a copy of the database, only while no P3 job is running, and never after 03:00 local, ahead of the 04:00 daily run that transcribes. Running a model beside a transcription crashed the Mac before.

**Fallback, not yet used:** if local runs can't fit around transcription, or `gemma4:12b` proves too weak for answers or tool calling, switch the query-time models to Gemini (embeddings and answers), which loads nothing on the Mac. The cost is a few dollars of API use and transcript passages sent to Google; the corpus would then be embedded with a Gemini model, so the switch means a full re-index (`embed_model` keeps the two apart). Decide this from the evaluation results.

## Phases

Each phase ends with a check that has to pass before the next one starts.

### Phase 0: measure (half a day)

- Peak memory during a Parakeet and a Whisper transcription, with `bge-m3` and the reranker loaded beside it. This decides whether questions can be answered **inline** while a transcription runs, or must **queue** behind it. The rule stays: nothing may risk the 16 GB machine.
- Chunk count for the current library, vector scan latency on that many rows, FTS rebuild time.
- Embedding throughput (chunks per second) to size the backfill.

**Check:** a short results section added to this document, and the inline-or-queued decision made.

### Phase 1: chunks and embeddings

- `p3/chunker.py`: segments to chunks, with prefix. `p3/embedder.py`: Ollama `/api/embed` in batches.
- An `embed` step after the digest in `task_digest` and `task_full_pipeline` (so the daily run gets it for free), an `embed` job type with retry support in `_retry_plan`, and an admin "Index older episodes" backfill on the Admin page, resumable by episode.
- `release_ollama_models` already unloads the embedding model before transcription.

**Check:** every processed episode has chunks with the current `embed_model`; deleting an episode leaves no chunks; tests for chunk boundaries, overlap and prefix.

### Phase 2: hybrid retrieval and reranking

- `p3/retriever.py`: BM25 and vector top 50, both scoped to the user and filters (podcast, date range, language), reciprocal rank fusion (k = 60), cross-encoder rerank, top ~12.
- `GET /api/ask/retrieve?q=` (admin): ranked passages with the score from each stage, no LLM. Used for debugging and by the evaluation.

**Check:** tests for fusion and scoping (another user's chunks never come back); a manual look at 10 questions.

### Phase 3: evaluation set and baseline

- Question generation: an LLM writes questions from randomly sampled chunks, each labelled with the chunks that answer it. The result is curated by hand (drop the bad ones) and topped up with ~20 real questions. Target: about 75 questions, a mix of English and French, factual, cross-episode ("compare what X and Y said"), and a few with no answer in the library.
- `p3/eval_ask.py`, run as an admin job: retrieval recall@12 and MRR, per stage (BM25 only, vector only, fused, reranked), written to `data/eval/results/<date>.json` and shown on the Admin page next to the previous run.

**Check:** a baseline result. Reranked recall@12 should beat both single methods; if it doesn't, fix retrieval before building answers on it.

### Phase 4: answers

- **Understand:** one LLM call returns JSON: a standalone question (rewritten from the thread for follow-ups), keywords in English and French, and filters. Parsed with `_extract_json`.
- **Answer:** the passages numbered `[c1]…[c12]`, each with podcast, episode, date and timestamp; the prompt requires a citation on every claim, an answer in the user's language, and an explicit "not in your library" reply when the passages don't answer.
- **Abstain** without calling the answer LLM when the best reranker score is below a threshold calibrated on the evaluation set's no-answer questions.
- **Validate:** citations that don't match a provided passage are removed; an answer left with none is flagged.
- **Stream:** `LLMClient.chat_stream` for Ollama, OpenAI and Gemini; `POST /api/ask` streams Server-Sent Events (stages, answer tokens, citations at the end). The frontend reads it with `fetch` and a stream reader, since `EventSource` can't POST.
- Each exchange is saved to `ask_messages`, with retrieval details, timings and model versions.

**Check:** the evaluation extended to answers: an LLM judge scores correctness against the reference and whether each claim is supported by its cited passage, spot-checked by hand on 20 answers. Citation validity at or near 100%, and no-answer questions correctly declined.

### Phase 5: Ask page

- `frontend/src/pages/Ask.jsx`: thread list, streaming answer, citation chips (podcast, episode, timestamp) linking to Episode Detail, a "Sources" panel with the passages, thumbs up/down.
- Episode Detail reads `?t=` and scrolls to and highlights that segment.
- Admin: recent questions with low ratings, with "Add to eval set".

**Check:** `npm run lint`; a question asked from the phone streams its first words within ~2 s with Gemini.

### Phase 6: tune

Change one thing at a time against the evaluation: chunk length (45/60/90 s), number of passages, the prefix, the abstain threshold, the reranker on or off. Keep what improves the numbers.

## Later, only if the evaluation shows the need

- An LLM-written context line per chunk instead of the episode summary prefix (expensive locally: one call per chunk).
- The `vss` HNSW index, if the scan gets slow.
- Exposing `/api/ask/retrieve` as an MCP server, to ask from Claude Desktop.

## Open questions

- Inline or queued answers while a transcription runs: Phase 0.
- Whether `sentence-transformers` (and torch) is acceptable as a dependency, or whether an MLX port of the reranker is mature enough to use instead.
- Whether the answer should follow the question's language or the user's `language` setting when they differ.

## Progress

- 2026-10-28: plan extended with the professional additions; work started on the `ask` branch.
- 2026-10-28, night: built on `ask` (nothing merged to main):
  - **Index** (Phase 1): `p3/chunker.py` (~60 s passages with one-segment overlap, plus show notes, summary, topics, quotes), `p3/embedder.py` (bge-m3 via Ollama; bge-reranker-v2-m3 via sentence-transformers, loaded lazily, released before transcription and after 10 idle minutes), `p3/indexer.py`, BM25 in SQL (`chunk_terms`). Indexing runs after every digest and as an admin backfill job (`POST /api/ask/index`) that yields to the daily run; a catch-up index job is queued behind each daily backlog.
  - **Retrieval** (Phase 2): `p3/retriever.py`, BM25 + vector top 50, reciprocal rank fusion, rerank of the top 40, at most 4 passages per episode, top 12. Each stage degrades on its own. Admin endpoint `GET /api/ask/retrieve`.
  - **Evaluation** (Phase 3): `p3/eval_ask.py` (question generation from passages, recall@12 and MRR per stage and stratum, paired bootstrap).
  - **Answers** (Phase 4): `p3/ask.py` (question step with keywords and filters, fenced passages, citation validation, abstain below a reranker threshold, streaming), threads and ratings stored with retrieval records, timings, model and prompt versions.
  - **Page** (Phase 5): `frontend/src/pages/Ask.jsx`; Episode Detail opens at `?t=seconds`.
  - **Show notes** (addition 2) stored and indexed.
  - **Transcription guard**: while transcription models are loaded, Ask answers with keyword search only and loads nothing.
  - Measured: indexing ≈ 6 passages/s with bge-m3 (~8–20 s per episode).
  - Measured, all local (gemma4:12b answers, bge-m3, reranker on Metal), 16 GB Mac with the usual apps open: free memory fell to 12–14% with swap nearly full, and answers took **100–210 s** (question step 12–26 s, reranking 13–27 s instead of the 1–3 s it takes without the 7.7 GB answer model resident, 80–170 s to the first answer token). Quality was good: grounded, cited, attributed to the right show. Ollama's default 32k context left 17% free; Ask now asks for 8k.
  - **Gemini fallback, evidence for the decision:** the time goes to memory pressure, not to the pipeline. Setting a user's *writing LLM* to Gemini (Settings, no code change) keeps embeddings and reranking local and sends only the retrieved passages (about 12 per question) to Google. Not switched: the owner chose local models.
