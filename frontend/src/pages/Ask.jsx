import { Fragment, useCallback, useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import {
  askStream,
  deleteAskThread,
  getPodcasts,
  getAskStatus,
  getAskThread,
  getAskThreads,
  indexLibrary,
  rateAskAnswer,
  stopAsk,
} from '../api/client';
import { useMe } from '../hooks/useMe';

const formatTime = (seconds) => {
  if (seconds == null) return '';
  const s = Math.floor(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
};

const STAGES = {
  understanding: 'Reading the question…',
  searching: 'Searching your library…',
  answering: 'Writing the answer…',
};

const KIND_LABELS = { summary: 'Episode summary', topics: 'Episode topics', quote: 'Quote', notes: 'Show notes' };
// Passages written by the digest model, not said in the episode.
const AI_WRITTEN = new Set(['summary', 'topics']);

// Where a source opens: the transcript at its moment (the whole passage
// highlighted), the summary tab, or the episode.
const sourceLink = (s) => {
  if (s.start_s != null) {
    const end = s.end_s != null ? `&end=${Math.ceil(s.end_s)}` : '';
    return `/episodes/${s.episode_id}?t=${Math.floor(s.start_s)}${end}`;
  }
  return AI_WRITTEN.has(s.kind) ? `/episodes/${s.episode_id}?tab=summary` : `/episodes/${s.episode_id}`;
};

const sourceLine = (s) =>
  [
    s.podcast_title,
    s.episode_title,
    s.date ? s.date.slice(0, 10) : null,
    s.start_s != null ? formatTime(s.start_s) : KIND_LABELS[s.kind],
  ]
    .filter(Boolean)
    .join(' · ');

// Answer text with [n] citations turned into links to their sources.
function AnswerText({ text, sources, weak }) {
  const byNumber = Object.fromEntries((sources || []).map((s) => [s.n, s]));
  const weakNumbers = new Set((weak || []).map((w) => w.n));
  const renderLine = (line, key) => {
    const parts = line.split(/(\[\d+\])/g);
    return (
      <Fragment key={key}>
        {parts.map((part, i) => {
          const match = /^\[(\d+)\]$/.exec(part);
          const source = match && byNumber[Number(match[1])];
          if (!source) return part;
          return (
            <Link
              key={i}
              to={sourceLink(source)}
              title={
                weakNumbers.has(source.n)
                  ? `${sourceLine(source)} — this passage may not support the sentence`
                  : sourceLine(source)
              }
              className={`inline-block align-super text-[10px] font-semibold rounded px-1 mx-0.5 no-underline ${
                weakNumbers.has(source.n)
                  ? 'text-amber-800 bg-amber-100 hover:bg-amber-200'
                  : 'text-blue-700 bg-blue-50 hover:bg-blue-100'
              }`}
            >
              {source.n}
            </Link>
          );
        })}
      </Fragment>
    );
  };
  const blocks = text.split(/\n{2,}/);
  return (
    <div className="space-y-3 text-sm text-gray-800 leading-relaxed">
      {blocks.map((block, b) => {
        const lines = block.split('\n');
        if (lines.every((l) => /^\s*[-*•]\s+/.test(l))) {
          return (
            <ul key={b} className="list-disc pl-5 space-y-1">
              {lines.map((l, i) => (
                <li key={i}>{renderLine(l.replace(/^\s*[-*•]\s+/, ''), i)}</li>
              ))}
            </ul>
          );
        }
        return (
          <p key={b}>
            {lines.map((l, i) => (
              <Fragment key={i}>
                {i > 0 && <br />}
                {renderLine(l.replace(/^\*\*(.+)\*\*$/, '$1'), i)}
              </Fragment>
            ))}
          </p>
        );
      })}
    </div>
  );
}

function SourceCard({ source }) {
  const [open, setOpen] = useState(false);
  const long = source.text.length > 280;
  return (
    <li className={`border rounded-lg p-3 ${source.cited ? 'border-blue-200 bg-blue-50/40' : 'border-gray-200'}`}>
      <div className="flex items-start gap-2">
        <span className="text-xs font-semibold text-blue-700 bg-blue-100 rounded px-1.5 py-0.5 shrink-0">
          {source.n}
        </span>
        <div className="min-w-0">
          <Link to={sourceLink(source)} className="text-xs font-medium text-blue-700 hover:underline break-words">
            {sourceLine(source)}
          </Link>
          {AI_WRITTEN.has(source.kind) && (
            <span
              className="ml-2 text-[10px] uppercase tracking-wide text-amber-800 bg-amber-50 border border-amber-200 rounded px-1"
              title="Written by the summarizing model from the transcript, not said in the episode. Check the transcript before quoting it."
            >
              AI summary
            </span>
          )}
          <p className="text-xs text-gray-600 mt-1 whitespace-pre-line break-words">
            {open || !long ? source.text : `${source.text.slice(0, 280)}…`}
          </p>
          {long && (
            <button onClick={() => setOpen(!open)} className="text-xs text-gray-500 hover:text-gray-700 mt-1">
              {open ? 'Show less' : 'Show the whole passage'}
            </button>
          )}
        </div>
      </div>
    </li>
  );
}

function Sources({ sources, mode }) {
  const [showOthers, setShowOthers] = useState(false);
  if (!sources?.length) return null;
  // With a written answer the cited passages come first and the rest stay
  // folded; when nothing matched they are only the nearest misses, folded;
  // otherwise (keyword results, waiting) every passage is a result.
  const cited =
    mode === 'answer' ? sources.filter((s) => s.cited) : mode === 'not_covered' ? [] : sources;
  const others =
    mode === 'answer' ? sources.filter((s) => !s.cited) : mode === 'not_covered' ? sources : [];
  const othersLabel = mode === 'not_covered' ? 'nearest passage' : 'other passage';
  return (
    <div className="mt-4">
      {cited.length > 0 && (
        <>
          <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">
            {mode === 'answer' ? 'Sources' : 'Passages found'}
          </h3>
          <ul className="space-y-2">
            {cited.map((s) => <SourceCard key={s.chunk_id} source={s} />)}
          </ul>
        </>
      )}
      {others.length > 0 && (
        <div className="mt-2">
          <button onClick={() => setShowOthers(!showOthers)} className="text-xs text-gray-500 hover:text-gray-700">
            {showOthers ? 'Hide' : 'Show'} {others.length} {othersLabel}{others.length === 1 ? '' : 's'}
          </button>
          {showOthers && (
            <ul className="space-y-2 mt-2">
              {others.map((s) => <SourceCard key={s.chunk_id} source={s} />)}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

// Feed text (titles, passages) is escaped for Markdown, so pasting the notes
// into Obsidian or Notion can't create links or load images from a feed;
// the answer keeps its [n] citations but loses any link or image syntax.
const escapeMd = (text) => text.replace(/([\\`*_[\]()!<>#|])/g, '\\$1');
const neutralizeLinks = (text) => text.replace(/!\[/g, '!\\[').replace(/\]\(/g, ']\\(');

// The answer with its cited sources as Markdown, for notes: the question as
// the search understood it, when it was asked, and each source quoted.
const asMarkdown = (question, message) => {
  const cited = (message.sources || []).filter((s) => s.cited || message.mode !== 'answer');
  const sources = cited.map((s) => {
    const badge = AI_WRITTEN.has(s.kind) ? ' (AI summary, not said in the episode)' : '';
    const quote = s.text.split('\n').map((line) => `> ${escapeMd(line)}`).join('\n');
    return `[${s.n}] ${escapeMd(sourceLine(s))}${badge} — <${window.location.origin}${sourceLink(s)}>\n${quote}`;
  });
  const asked = new Date().toISOString().slice(0, 10);
  const warning = message.uncited ? '\n\n*This answer cites no passage.*' : '';
  return `## ${escapeMd(message.standalone || question)}\n\n*Asked ${asked}, from my podcast library.*\n\n${neutralizeLinks(message.answer)}${warning}\n\n${sources.join('\n\n')}\n`;
};

// "Searched: …" under an answer, so a narrowed search is never invisible.
function ScopeLine({ scope }) {
  if (!scope) return null;
  const shows = scope.podcasts?.length ? `${scope.podcasts.join(', ')} only` : 'all shows';
  const dates =
    scope.after || scope.before
      ? [scope.after && `from ${scope.after}`, scope.before && `to ${scope.before}`].filter(Boolean).join(' ')
      : 'all dates';
  const coverage =
    scope.episodes_indexed < scope.episodes_processed
      ? ` · ${scope.episodes_indexed} of ${scope.episodes_processed} episodes indexed`
      : ` · ${scope.episodes_indexed} episodes`;
  return (
    <p className="text-[11px] text-gray-500 mt-3">
      Searched: {shows}, {dates}
      {scope.inferred && ' (read from your question)'}
      {scope.chosen && ' (your filters)'}
      {coverage}
    </p>
  );
}

const NOTICES = {
  paused: 'An episode is being transcribed: these are keyword matches only.',
  llm_failed: 'The answer model failed; the passages it would have used are below.',
  no_llm: 'No answer model is configured; these are the passages found.',
};

function AssistantMessage({ message, question }) {
  const [rating, setRating] = useState(message.rating || 0);
  const [copied, setCopied] = useState(false);
  const rate = async (value) => {
    const next = rating === value ? 0 : value;
    setRating(next);
    await rateAskAnswer(message.id, next).catch(() => setRating(rating));
  };
  const copy = async () => {
    try {
      if (!navigator.clipboard) throw new Error('no clipboard');
      await navigator.clipboard.writeText(asMarkdown(question, message));
      setCopied('Copied');
    } catch {
      setCopied('Copy failed: your browser blocked the clipboard');
    }
    setTimeout(() => setCopied(false), 2500);
  };
  return (
    <div className="bg-white border rounded-xl p-4 shadow-sm">
      {NOTICES[message.mode] && (
        <p className="text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded px-2 py-1 mb-3">
          {NOTICES[message.mode]}
        </p>
      )}
      {message.answer && (
        <AnswerText text={message.answer} sources={message.sources} weak={message.weak} />
      )}
      {message.weak?.length > 0 && (
        <p className="text-xs text-amber-800 mt-2">
          {message.weak.length === 1 ? 'One citation (in amber) points' : `${message.weak.length} citations (in amber) point`} to a
          passage that may not support its sentence: check it before relying on it.
        </p>
      )}
      {message.uncited && (
        <p className="text-xs text-amber-700 mt-2">
          This answer cites no passage: check it against the sources before relying on it.
        </p>
      )}
      <Sources sources={message.sources} mode={message.mode} />
      <ScopeLine scope={message.scope} />
      {message.id && (
        <div className="flex items-center gap-3 mt-3 text-xs text-gray-500">
          <button onClick={() => rate(1)} className={rating === 1 ? 'text-green-700 font-semibold' : 'hover:text-gray-800'} aria-label="Helpful">
            ▲ Helpful
          </button>
          <button onClick={() => rate(-1)} className={rating === -1 ? 'text-red-700 font-semibold' : 'hover:text-gray-800'} aria-label="Not helpful">
            ▼ Not helpful
          </button>
          <button onClick={copy} className="hover:text-gray-800 ml-auto">
            {copied || 'Copy with sources'}
          </button>
        </div>
      )}
    </div>
  );
}

// While an answer is written: the stage, the time so far, and the passages
// as soon as the search has found them, so there is something to read.
function Pending({ stage, sources, startedAt }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const seconds = Math.round((now - (startedAt || now)) / 1000);
  return (
    <div className="bg-white border rounded-xl p-4">
      <p className="text-sm text-gray-500">
        <span className="animate-pulse">{STAGES[stage] || 'Working…'}</span>{' '}
        <span className="tabular-nums">{seconds} s</span>
      </p>
      {sources?.length > 0 && (
        <>
          <p className="text-xs text-gray-500 mt-1">
            With a local model an answer can take a few minutes. The passages it will use are ready to read now.
          </p>
          <Sources sources={sources} mode="passages" />
        </>
      )}
    </div>
  );
}

// A stored assistant message in the shape the page uses.
const fromStored = (m) => ({
  id: m.id,
  role: m.role,
  answer: m.content,
  content: m.content,
  sources: m.citations || [],
  mode: m.mode || 'answer',
  scope: m.scope,
  standalone: m.standalone,
  weak: m.weak_citations,
  uncited: (m.mode || 'answer') === 'answer' && m.role === 'assistant' && !m.citations?.some((c) => c.cited),
  rating: m.rating,
});

export default function Ask() {
  const { me } = useMe();
  const [params, setParams] = useSearchParams();
  const threadId = params.get('thread');
  const [threads, setThreads] = useState([]);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState(null);
  const [error, setError] = useState(null);
  const [status, setStatus] = useState(null);
  const [showThreads, setShowThreads] = useState(false);
  // Optional limits the user sets; empty means the question decides.
  const [podcasts, setPodcasts] = useState([]);
  const [filters, setFilters] = useState({ podcast: '', after: '', before: '' });
  const [showFilters, setShowFilters] = useState(false);
  const activeFilters = Boolean(filters.podcast || filters.after || filters.before);
  const bottomRef = useRef(null);
  const abortRef = useRef(null);

  const loadThreads = useCallback(() => getAskThreads().then(setThreads).catch(() => {}), []);
  useEffect(() => { loadThreads(); }, [loadThreads]);
  useEffect(() => { getAskStatus().then(setStatus).catch(() => {}); }, []);
  useEffect(() => { getPodcasts().then(setPodcasts).catch(() => {}); }, []);

  useEffect(() => {
    if (busy) return;
    if (!threadId) {
      setMessages([]);
      return;
    }
    // An answer still being written (the page was closed meanwhile): show it
    // as pending and check back until it's stored.
    let timer;
    let cancelled = false;
    const load = () =>
      getAskThread(threadId)
        .then((t) => {
          if (cancelled) return;
          const stored = t.messages.map(fromStored);
          setMessages(
            t.answering
              ? [...stored, { role: 'assistant', answer: '', sources: [], streaming: true, startedAt: Date.now() }]
              : stored
          );
          if (t.answering) timer = setTimeout(load, 4000);
        })
        .catch((e) => setError(e.message));
    load();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // Only when the thread changes; a running answer updates messages itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end' });
  }, [messages.length, stage]);

  const submit = async (e, text) => {
    e?.preventDefault();
    const question = (text ?? input).trim();
    if (!question || busy) return;
    setInput('');
    setError(null);
    setBusy(true);
    setStage('understanding');
    const pending = {
      role: 'assistant',
      answer: '',
      sources: [],
      mode: 'answer',
      streaming: true,
      startedAt: Date.now(),
    };
    setMessages((prev) => [...prev, { role: 'user', content: question }, pending]);
    const update = (patch) =>
      setMessages((prev) => [...prev.slice(0, -1), { ...prev[prev.length - 1], ...patch }]);
    const controller = new AbortController();
    abortRef.current = controller;
    let streamed = '';
    let finished = false;
    let stored = false;
    try {
      await askStream(
        question,
        threadId,
        (event) => {
          if (event.event === 'thread') {
            stored = true;
            // Stored already: from here the page can be left and come back.
            if (event.thread_id !== threadId) setParams({ thread: event.thread_id }, { replace: !threadId });
          } else if (event.event === 'status') setStage(event.stage);
          else if (event.event === 'sources') update({ sources: event.sources });
          else if (event.event === 'token') {
            streamed += event.text;
            update({ answer: streamed });
          } else if (event.event === 'done') {
            finished = true;
            update({
              id: event.message_id,
              answer: event.answer,
              sources: event.sources,
              mode: event.mode,
              scope: event.scope,
              standalone: event.understanding?.question,
              weak: event.weak_citations,
              uncited: event.uncited_answer,
              streaming: false,
            });
            if (event.thread_id !== threadId) setParams({ thread: event.thread_id }, { replace: !threadId });
          } else if (event.event === 'error') {
            finished = true;
            // The question and this failure are stored in the thread: stay in it.
            if (event.thread_id !== threadId) setParams({ thread: event.thread_id }, { replace: !threadId });
            throw new Error(event.detail);
          }
        },
        controller.signal,
        {
          podcast_ids: filters.podcast ? [Number(filters.podcast)] : undefined,
          after: filters.after || undefined,
          before: filters.before || undefined,
        }
      );
      // The connection dropped before the answer ended: it is still being
      // written and stored server-side; reopening the thread shows it.
      if (!finished) throw new Error('The connection was lost; the answer will appear when you reopen this conversation.');
    } catch (err) {
      if (err.name !== 'AbortError') setError(err.message);
      if (!stored) {
        // Refused (e.g. another answer is being written): take it back off.
        setMessages((prev) => prev.slice(0, -2));
        setInput(question);
      } else {
        update({ streaming: false });
      }
    } finally {
      setBusy(false);
      setStage(null);
      loadThreads();
    }
  };

  const removeThread = async (id) => {
    await deleteAskThread(id).catch((e) => setError(e.message));
    if (id === threadId) setParams({});
    loadThreads();
  };

  const index = status?.index;
  // Example questions from the user's own recent topics, else generic ones.
  const examples = status?.suggestions?.length
    ? status.suggestions.map((topic) => `What have my podcasts said about ${topic}?`)
    : [
        'What did my podcasts say this month about the topic I follow most?',
        'Which guests disagreed with each other, and on what?',
        'Quels épisodes récents parlent de santé?',
      ];
  const partlyIndexed = index && index.episodes < index.processed_episodes;
  const questionFor = (i) => messages[i - 1]?.content || '';

  return (
    <div className="max-w-5xl flex flex-col lg:flex-row gap-6">
      <aside className="lg:w-56 shrink-0">
        <div className="flex items-center justify-between lg:block">
          <button
            onClick={() => { setParams({}); setShowThreads(false); }}
            disabled={busy}
            className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 lg:w-full"
          >
            New question
          </button>
          <button onClick={() => setShowThreads(!showThreads)} className="lg:hidden text-sm text-gray-600">
            {showThreads ? 'Hide' : 'Past'} conversations ({threads.length})
          </button>
        </div>
        <ul className={`${showThreads ? 'block' : 'hidden'} lg:block mt-3 space-y-1`}>
          {threads.map((t) => (
            <li key={t.id} className="group flex items-center gap-1">
              <button
                onClick={() => { setParams({ thread: t.id }); setShowThreads(false); }}
                disabled={busy}
                className={`flex-1 text-left text-sm truncate px-2 py-1 rounded ${t.id === threadId ? 'bg-gray-200 text-gray-900' : 'text-gray-600 hover:bg-gray-100'}`}
                title={t.title}
              >
                {t.title}
              </button>
              <button
                onClick={() => removeThread(t.id)}
                className="text-xs text-gray-400 hover:text-red-600 lg:opacity-0 group-hover:opacity-100"
                aria-label="Delete conversation"
              >
                ✕
              </button>
            </li>
          ))}
        </ul>
      </aside>

      <section className="flex-1 min-w-0">
        <h1 className="text-2xl font-bold text-gray-900 mb-1">Ask your library</h1>
        <p className="text-sm text-gray-500 mb-4">
          Answers come only from your podcasts. Each claim links to the transcript moment, or the episode summary, it comes from.
        </p>

        {partlyIndexed && (
          <div className="text-xs text-gray-600 bg-gray-50 border rounded px-3 py-2 mb-4 flex items-center gap-3">
            <span>
              {index.episodes} of {index.processed_episodes} episodes are indexed; answers can only use those.
            </span>
            {me?.role === 'admin' && (
              <button
                onClick={() => indexLibrary().then(() => setStatus({ ...status, indexing: true }))}
                disabled={status.indexing}
                className="ml-auto px-2 py-1 bg-white border rounded hover:bg-gray-100 disabled:opacity-50"
              >
                {status.indexing ? 'Indexing queued' : 'Index the rest'}
              </button>
            )}
          </div>
        )}
        {status?.models_paused && (
          <p className="text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded px-3 py-2 mb-4">
            An episode is being transcribed right now, so answers are limited to keyword matches until it finishes.
          </p>
        )}

        {messages.length === 0 && (
          <div className="mb-6">
            <p className="text-sm text-gray-600 mb-2">Try:</p>
            <div className="flex flex-col gap-2">
              {examples.map((q) => (
                <button
                  key={q}
                  onClick={(e) => submit(e, q)}
                  className="text-left text-sm text-blue-700 bg-blue-50 hover:bg-blue-100 rounded-lg px-3 py-2"
                >
                  {q}
                </button>
              ))}
            </div>
          </div>
        )}

        <div className="space-y-4 mb-4">
          {messages.map((m, i) =>
            m.role === 'user' ? (
              <div key={i} className="flex justify-end">
                <p className="bg-gray-900 text-white text-sm rounded-xl px-4 py-2 max-w-[85%] whitespace-pre-line">
                  {m.content}
                </p>
              </div>
            ) : (
              <div key={m.id || i}>
                {m.streaming && !m.answer ? (
                  <Pending stage={stage} sources={m.sources} startedAt={m.startedAt} />
                ) : (
                  <AssistantMessage message={m} question={questionFor(i)} />
                )}
              </div>
            )
          )}
          <div ref={bottomRef} />
        </div>

        {error && <p className="text-sm text-red-600 mb-3">{error}</p>}

        <form onSubmit={submit} className="sticky bottom-0 bg-gray-50 pt-2 pb-4">
          <div className="flex items-center gap-2 mb-2 text-xs">
            <button
              type="button"
              onClick={() => setShowFilters(!showFilters)}
              className={`px-2 py-1 rounded border ${activeFilters ? 'border-blue-300 bg-blue-50 text-blue-800' : 'text-gray-600 hover:bg-gray-100'}`}
            >
              {activeFilters ? 'Filters on' : 'Filters'}
            </button>
            {activeFilters && (
              <button
                type="button"
                onClick={() => setFilters({ podcast: '', after: '', before: '' })}
                className="text-gray-500 hover:text-gray-800"
              >
                Clear
              </button>
            )}
          </div>
          {showFilters && (
            <div className="flex flex-wrap items-end gap-3 mb-2 text-xs">
              <label className="flex flex-col gap-1">
                Show
                <select
                  value={filters.podcast}
                  onChange={(e) => setFilters({ ...filters, podcast: e.target.value })}
                  className="border rounded px-2 py-1 max-w-[16rem]"
                >
                  <option value="">All shows</option>
                  {podcasts.map((p) => (
                    <option key={p.id} value={p.id}>{p.title}</option>
                  ))}
                </select>
              </label>
              <label className="flex flex-col gap-1">
                From
                <input type="date" value={filters.after} onChange={(e) => setFilters({ ...filters, after: e.target.value })} className="border rounded px-2 py-1" />
              </label>
              <label className="flex flex-col gap-1">
                To
                <input type="date" value={filters.before} onChange={(e) => setFilters({ ...filters, before: e.target.value })} className="border rounded px-2 py-1" />
              </label>
            </div>
          )}
          <div className="flex gap-2">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) submit(e);
              }}
              rows={2}
              maxLength={2000}
              placeholder={messages.length ? 'Ask a follow-up…' : 'Ask about anything said in your podcasts…'}
              className="flex-1 border rounded-lg px-3 py-2 text-sm resize-none focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            {busy ? (
              <button
                type="button"
                onClick={() => stopAsk().catch((e) => setError(e.message))}
                title="Stops the answer where it is and keeps what was written"

                className="px-4 py-2 border text-sm rounded-lg hover:bg-gray-100"
              >
                Stop
              </button>
            ) : (
              <button
                type="submit"
                disabled={!input.trim()}
                className="px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 disabled:opacity-50"
              >
                Ask
              </button>
            )}
          </div>
        </form>
      </section>
    </div>
  );
}
