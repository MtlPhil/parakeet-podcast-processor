# Ask: natural-language questions over the library

**Status: built (2026-10-28 to 10-30), merged into `main` with step 6 of the roadmap (2026-11-11).** This document records the plan and the decisions made so far; [Progress](#progress) says what is built and what is left. Speaker names (the speakers phase) are not merged: they stay on the `speakers` branch until finished. It is item 3 of the direction in [ROADMAP.md](../ROADMAP.md), which adds two things this plan does not cover: the web as a second source next to the library, and tool calls for every provider.

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

### Phase 7: speakers

Two separate problems: **diarization** (when each voice speaks, as anonymous labels) works on the audio; **naming** (who each label is) works on the text.

1. **Diarize.** A diarization model (pyannote, or sherpa-onnx if pyannote is too heavy) runs on the 16 kHz mono WAV transcription already uses: it finds speech, embeds short windows as voice fingerprints and clusters them into speakers. Output: turns (`start`, `end`, label). Language-independent. A hint of the expected number of speakers helps solo shows that would otherwise split on a change of microphone or tone.
2. **Align.** Each transcript segment gets the speaker who covers most of its time (the segments' `speaker` field, empty today). A segment where the turn changes midway, or with overlapping speech, is marked uncertain rather than given to one person. Parakeet's word timestamps allow splitting at the turn later.
3. **Name.** One LLM call per episode: show notes, episode and podcast title, and each label's first lines (introductions, "thanks for having me, Lenny"). It returns a name or "unknown" per label, never a guess. Later: keep each named host's voice fingerprint per podcast and recognize them in later episodes without an LLM call, leaving only guests to name from the text.
4. **Store and use.** Speakers on segments; a per-episode table of label, name, source (show notes, introduction, voice match) and confidence; names editable on Episode Detail. Passages are reindexed with their speaker, citations read "*Name, 14:32*", "what did X say" becomes a speaker filter, and the answer prompt's "in the conversation with X" rule gives way to direct attribution for named, confident segments only.
5. **Where it runs.** A queued job between transcription and digest, since the digest deletes the audio; same memory rules as transcription (Ollama models released, Ask paused). Processed episodes have no audio left: diarizing them means downloading it again, either as a slow background backfill or not at all (new episodes only).
6. **Check.** Hand-mark turns on 3 or 4 varied episodes (a two-person interview, a panel, a solo news brief, a French show); measure diarization error rate and naming accuracy. The test that matters: host lines no longer credited to guests. Known hard cases: ads read by the host, intros over music, callers, crosstalk.

**First step:** run pyannote by hand on those episodes, outside the server and with nothing else loaded, to measure time per hour of audio, peak memory and quality. That decides whether it fits on the 16 GB Mac, or whether sherpa-onnx is needed, before anything is built.

**Measured (2026-10-29, pyannote 4.0.7 `speaker-diarization-community-1`, torch 2.15, separate venv, nothing else loaded):**

| Episode | Length | Device | Time | Speakers with real talk time | Peak memory |
|---|---|---|---|---|---|
| RAG Explained (solo) | 12 min | GPU (MPS) | 63 s | 1 (712 s; 3 blips under 0.5 s) | 5.5 GB, free fell to 36% |
| same | 12 min | CPU | 461 s | same result | 2.3 GB, free unchanged |
| Joëlle Pineau, Les Dérangeants (fr) | 44 min | GPU | 211 s | 3 (two hosts, guest) + ad, jingle, teaser voices | 5.6 GB, 35% |
| Zach Hanif, Control on AI (en) | 52 min | GPU | 248 s | 2 (host, guest) | |
| Gaëtan Namouric, Les Dérangeants (fr) | 89 min | GPU | 428 s | 3 + four small intro voices | 7.3 GB for the 52 + 89 min run, 25% |

- **Speed:** 0.08× real time on the GPU (about 5 minutes per hour of audio), 0.64× on the CPU (about 38). Model load 1–7 s.
- **Memory:** 5.5 GB on the GPU up to 44 min, 7.3 GB in the process that did 52 then 89 min (not measured separately). It fits only with the transcription models and Ollama unloaded: a queued job of its own, like transcription. The CPU takes 2.3 GB and could run beside other work, at 7× the time.
- **Quality**, checked by transcribing 2–4 minute windows with Whisper and labelling each sentence with the speaker covering most of it: in both interviews every question and answer in the windows went to the right voice, including the cold open (the host's questions as a montage) and the sponsor read (its own voice). Misses were only at handovers inside one long Whisper sentence (e.g. "Salut, Étienne." given to the host), so align by word timestamps (Parakeet has them), not by segment. About 100–250 turns under 1 s per episode are overlap blips: drop them before aligning.
- **Naming:** the evidence is in the text: "Étienne Crevier à l'animation", "Dominique Gagnon, bonjour", "Thank you for having me, Ash", the guest named in the intro. Show-level voices (sponsor read, jingle) recur every episode: label them "ad" or "intro" rather than a person.
- **Setup:** the model is gated on Hugging Face (accept its terms, a read token on the server). The project venv has torch 2.14 (from the reranker); pyannote 4.0.7 was tested with 2.15, so the versions need reconciling, or a separate environment run as a subprocess.

**Decision to make:** GPU in the serial queue between transcription and digest (about 5 min per hour, models unloaded around it) is the recommended route; sherpa-onnx isn't needed.

**Built (2026-10-30 on the `speakers` branch; ported to `main` 2026-11-14):** `p3/speakers.py`.
- **Where it runs:** inside the transcription step rather than as its own job: after transcribing, the transcription models are unloaded, then pyannote runs on the same audio while Ask is still paused and Ollama still released. No new status, job type or retry path. Without the `speakers` extra, if the model can't load, or if diarization fails, the transcript is stored without speakers as before. Shared settings `diarization_enabled` (default true when installed) and `diarization_model`.
- **Align:** both MLX engines hand over word timings (Whisper's words, Parakeet's tokens). Each word gets the turn covering most of it; segments are split at speaker changes; pieces under 2 words or 0.5 s join their neighbour; crosstalk blips under 1 s inside another turn are dropped first. faster-whisper reports no words, so off the Mac a segment gets one speaker, unsplit.
- **Name:** at the digest step, once per episode: show notes, the first 6 minutes as a labelled conversation, up to 3 later lines of each speaker and, for each voice first heard after the opening (the 5 that talk most), the conversation around its first turn (the minute before, where a host usually introduces it, and its first 30 s; about 1,500 characters, some 400 tokens), about 2,500 tokens of text in all plus that, no audio. The LLM is the shared `speaker_naming_provider`'s (here Gemini; unset: the digest's own). A name is kept only if each of its words appears in that text, or a word one or two letters away (the transcript may misspell a name), and only once. Roles: host, guest, ad, intro, other. Stored in `episode_speakers`; a new transcript clears them. At Gemini Flash's late-2026 prices, under half a cent per episode.
- **Use:** the digest and synopsis read the transcript with "Name: " starting each turn; Ask passages too, with `chunks.speakers`; citations read "Podcast · Episode · date · Name, 14:32"; the answer prompt (answer-v5) attributes named lines directly, treats "Speaker 1" and unnamed passages as before, and ignores "Ad:" and "Intro:"; the episode page shows names over the transcript.
- **Tested end to end (2026-10-30)** on two episodes with Whisper: 44 min diarized in 204 s, 52 min in 245 s, free memory no lower than 27%.
- **Real run on `main` (2026-11-14):** the first 8 minutes of a Les Dérangeants episode (French, no show notes), in a separate process on a scratch database: Whisper and diarization in 115 s (diarization 41 s, 37 turns, 361 of 362 segments with a speaker), free memory no lower than 62%. Gemini named both hosts and the guest, and marked the sponsor read and the jingle "Ad" and "Intro". The first try dropped the co-host: Whisper wrote "Abattiello", the model "Abbatiello", so the near-spelling rule was added. Gemini 3.8 Flash was overloaded both times; the fallback model answered after 3 to 6 minutes of retries.

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
  - **Retrieval baseline** (Phase 3 check; 33 questions written from sampled passages: 26 lookups incl. 3 French, 3 cross-language, 4 with no answer; in gitignored `data/eval/`): the right passage is in the top 12 for all 29 answerable questions. MRR by stage: BM25 0.78, vector 0.84, fused 0.86, **reranked 0.94**. The reranker's gain over fusion is +0.08 MRR (95% CI 0.01 to 0.16, paired bootstrap). French: BM25 alone finds 60%, the pipeline 100% (MRR 0.83). Cross-language: 100% (MRR 0.72). Abstain margin: no-answer questions score at most 0.009, answerable ones at least 0.63, so `ABSTAIN_THRESHOLD = 0.05` is safe. Median search 3.7 s with the reranker alone in memory. Caveat: questions written from the passages are easier than real ones; extend with real and rated questions.
  - **Cross-language fix:** a French question about English episodes scored 0.009 at the reranker (wrongly declined); the question step now also translates the question, and the translation is reranked too when the original scores low: 0.41, right podcast.
  - Local answer runs were stopped at 00:25 when free memory fell to 5% (the reranker, bge-m3 and gemma4:12b loading together, with other apps open). 9 of 16 real questions were answered; the other 7 were checked for retrieval and declining only.
  - **Gemini fallback, evidence for the decision:** the time goes to memory pressure, not to the pipeline. Setting a user's *writing LLM* to Gemini (Settings, no code change) keeps embeddings and reranking local and sends only the retrieved passages (about 12 per question) to Google. Not switched: the owner chose local models.
- 2026-10-29, night (continued): two red-team passes (21 confirmed defects, each with a failing probe test, all fixed; probes kept in `tests/test_ask_redteam*.py`) and a client-experience review from a research listener's point of view (attribution, AI-summary citations, padded broad answers, the wait, hidden scope, copy to notes; fixed except the larger items below). Added since: rerank with the question's translation (cross-language), manual show and date filters, answers that finish and are stored when the page is closed, passages shown while the answer is written, "AI summary" badges, quotes placed at their transcript moment, French summaries for French episodes.

- 00:55: a final guarded verification run (3 questions, reranker released before each answer) was killed by its watchdog when free memory fell to **1%** as gemma4:12b loaded beside the Ask models (it started at 80% free). No harm done; the P3 server kept running. The new prompt rules are therefore **not yet verified on a real broad answer**. Ask now refuses to load a local answer model unless free memory exceeds its size plus 5.5 GB (`room_for_ollama_model`; floor `ask_min_free_memory`, default 25%), and returns the passages instead.

- 2026-10-29, evening: **counting questions.** The question step names what a "how often", "how many" or "which episodes" question counts (`mentions`, with spellings and translations); `P3Database.mention_counts` finds every episode of the user's library, within the filters, whose transcript has a passage with all the words of one of them. The answer model gets that count and list (30 episodes, fenced) before the passages and takes numbers from it; the page shows the full list (up to 100) under the answer. A count with matches is answered even when no passage passes the abstain threshold. Not yet tried on the live library. Also fixed the scheduler test that failed between 23:45 and 04:00 (it read the real clock).
- 2026-10-30: speakers built on the `speakers` branch (diarization, alignment, names); not finished, not merged.
- 2026-11-11: merged into `main` as step 6 of the roadmap, on top of the restructure: Ask's tables are database migration 2, its storage is `p3/db/ask.py`, and transcription pauses Ask through the shared transcribe step. Answers can also come from Anthropic or any OpenAI-compatible server (both answer whole rather than streaming). The branch history is kept on the `speakers` branch.
- 2026-11-14: speakers ported to `main` (migration 5) with speaker naming on its own LLM setting, checked on a real episode (see Phase 7).
- 2026-11-12: a question that waited 9 minutes on an overloaded Gemini came back to an empty page: Cloudflare, in front of the web app, closes a connection with no data for about 100 s. The stream now sends a keep-alive line every 15 s, the page explains a slow first step after 30 s, and an overloaded Gemini model is skipped by every client for 30 minutes after it fails all its retries.

## Trying it

1. Stop the server. Install with the reranker: `uv sync --frozen --extra web --extra ask` (or `pip install -e ".[web,ask]"`), `ollama pull bge-m3`, and `cd frontend && npm run build`.
2. Start the server. The first open runs migration 2 (the new tables) and checkpoints at once.
3. Admin → Ask quality → **Index the library**: a queued job, about 15 to 20 minutes for 100 episodes, which gives way to the daily run.
4. Ask a few questions. With gemma4:12b expect 1.5 to 3 minutes per answer, and often a "not enough free memory" reply with the passages only: on this Mac the 12B model barely fits beside Ask's models. Setting your **writing LLM to Gemini** in Settings makes answers take seconds and avoids the memory problem (see the measurements above).
5. Admin → Ask quality → **Evaluate retrieval** reruns the baseline on the live library (the question set is in `data/eval/questions.json`).

## Still open

- **Decide local vs Gemini for answers**, from the measurements above, or try a smaller local answer model. Then check the new attribution rules on a broad question (the run meant to verify them was stopped by the memory watchdog).
- **Speakers:** built and in `main`. Late voices: since 2026-11-14 the naming request also has the conversation around each late voice's first turn (untried on a real episode with a late guest). "What did X say" (2026-11-14): the question step names the person, matched to the library's speaker names, and only passages they speak in are searched (a filter read from the question, dropped when it leaves nothing, so episodes without speakers don't hide the answer). Left: editing a name on the episode page; a show's host voice remembered across episodes; episodes digested before have no audio left, so no speakers.
- **Counting by concept** rather than by word: the episode count matches the words the question step names (and their spellings and translations), so an episode that discusses the idea in other words isn't counted. Check the count on real questions once answers run.
- **The support check** flags only clear mismatches (1 citation in 80 on the night's answers); measure subtler overstatement with `python -m p3.eval_ask answers`.
- **Tools** for the model (addition 3, beyond the broad-question routing done).
- Touch-friendly citation popovers; flagging a single citation. (Done 2026-11-14: sponsor reads and jingles, by their speaker role, rank last; a cold open's passages, the first 3 minutes' clips said again later, are not indexed. Both apply to episodes indexed from now on; the cold-open check is untried on a full real episode.)
- Answers run on daemon threads: a server restart mid-answer leaves the question without a reply (rare; the next question works).
