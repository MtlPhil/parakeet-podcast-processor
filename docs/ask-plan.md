# Ask: natural-language questions over the library

**Status: planned (2026-10-28).** Nothing has been built yet. This document records the plan and the decisions made so far.

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
| Keyword search | DuckDB `fts` extension (BM25), accents stripped, no stemmer. The query step produces keywords in both English and French, which covers both languages without one index per language |
| Vector search | Embeddings in a `FLOAT[1024]` column, brute-force `array_cosine_similarity`. A personal library (~100k chunks) scans in well under a second; the `vss` HNSW index is only worth adding if Phase 0 measures otherwise |
| Embedding model | `bge-m3` through Ollama: multilingual, so a French question finds English passages and vice versa, 8k-token input, 1024 dimensions. Shared setting (`embedding_model`), since the corpus is embedded once for everyone |
| Reranker | `bge-reranker-v2-m3` cross-encoder (multilingual), through `sentence-transformers`, in a new optional `ask` extra with a guarded import (`RERANKER_AVAILABLE`). Without it, retrieval falls back to the fused order |
| Answer LLM | The user's writing LLM (`llm_provider` / `llm_model` / `gemini_model`), no new setting |
| Chunks | Transcript segments grouped into ~60 s passages on segment boundaries, one segment of overlap, plus the episode's summary, topics and quotes as their own chunks. Each chunk is embedded with a prefix: podcast, episode title, date, and the episode's short summary |
| Scoping | Every retrieval query joins `user_episodes`, like `search`. Deleting an episode deletes its chunks |
| Deep links | Citations open Episode Detail scrolled to the cited segment (`/episodes/:id?t=<seconds>`). Audio is deleted after the digest, so there is no playback |
| Evaluation | A question set kept in gitignored `data/eval/` (it's derived from the library, which stays out of this public repo), run as an admin job. Pure helpers (chunking, fusion, citation parsing) get unit tests in CI as usual |

## Data model

- **`chunks`**: `id`, `episode_id`, `kind` (`transcript` | `summary` | `topics` | `quote`), `start`, `end` (seconds, transcript chunks only), `text`, `prefix`, `language`, `embedding FLOAT[1024]`, `embed_model`, `created_at`. `embed_model` lets a model change re-index in the background instead of mixing incomparable vectors.
- **FTS index** on `chunks.text`. DuckDB's FTS index doesn't update itself: it is rebuilt at the end of every embedding job (to measure in Phase 0).
- **`ask_threads`** (`id`, `username`, `title`, `created_at`) and **`ask_messages`** (`thread_id`, `role`, `content`, `citations` JSON, `retrieval` JSON with chunk IDs and scores per stage, `timings` JSON, `models` JSON, `rating`).
- `purge_episode` adds `chunks` to the tables it clears.

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
