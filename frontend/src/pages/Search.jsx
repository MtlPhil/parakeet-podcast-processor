import { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { search } from '../api/client';

const formatTime = (seconds) => {
  if (seconds == null) return '';
  const s = Math.floor(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
};

// Wraps every case-insensitive occurrence of the query in <mark>.
function Highlight({ text, query }) {
  if (!query) return text;
  const lower = text.toLowerCase();
  const q = query.toLowerCase();
  const parts = [];
  let i = 0;
  for (let pos = lower.indexOf(q); pos !== -1; pos = lower.indexOf(q, i)) {
    if (pos > i) parts.push(text.slice(i, pos));
    parts.push(<mark key={pos} className="bg-yellow-200 rounded px-0.5">{text.slice(pos, pos + q.length)}</mark>);
    i = pos + q.length;
  }
  parts.push(text.slice(i));
  return parts;
}

export default function Search() {
  const [params, setParams] = useSearchParams();
  const query = params.get('q') || '';
  const [input, setInput] = useState(query);
  // The response for the last query sent: { query, results, error }.
  const [fetched, setFetched] = useState(null);
  const valid = query.trim().length >= 2;

  useEffect(() => {
    if (!valid) return;
    let cancelled = false;
    search(query)
      .then((results) => { if (!cancelled) setFetched({ query, results, error: null }); })
      .catch((e) => { if (!cancelled) setFetched({ query, results: null, error: e.message }); });
    return () => { cancelled = true; };
  }, [query, valid]);

  const current = valid && fetched?.query === query ? fetched : null;
  const loading = valid && !current;
  const results = current?.results;
  const error = current?.error;

  const submit = (e) => {
    e.preventDefault();
    setParams(input.trim() ? { q: input.trim() } : {});
  };

  return (
    <div className="max-w-3xl">
      <h1 className="text-2xl font-bold text-gray-900 mb-6">Search</h1>

      <form onSubmit={submit} className="flex gap-2 mb-6">
        <input
          type="search"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Search transcripts, summaries and episode titles"
          className="flex-1 min-w-0 border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
        <button
          type="submit"
          disabled={input.trim().length < 2}
          className="px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 disabled:opacity-50"
        >
          Search
        </button>
      </form>

      {loading && <p className="text-sm text-gray-500">Searching...</p>}
      {error && <p className="text-sm text-red-600">{error}</p>}
      {results && (
        <p className="text-sm text-gray-500 mb-4">
          {results.length === 0
            ? `No episodes match "${query}".`
            : `${results.length} episode${results.length === 1 ? '' : 's'} match "${query}".`}
        </p>
      )}

      <div className="space-y-4">
        {results?.map((r) => (
          <div key={r.episode_id} className="bg-white border rounded-lg p-4">
            <Link to={`/episodes/${r.episode_id}`} className="font-medium text-blue-700 hover:underline">
              <Highlight text={r.episode_title} query={query} />
            </Link>
            <p className="text-xs text-gray-500 mt-0.5">
              {r.podcast_title}
              {r.date && <> &middot; {new Date(r.date).toLocaleDateString()}</>}
              {r.segment_count > 0 && (
                <> &middot; {r.segment_count} transcript match{r.segment_count === 1 ? '' : 'es'}</>
              )}
            </p>
            {r.summary_snippet && (
              <p className="text-sm text-gray-700 mt-2">
                <span className="text-xs font-medium text-gray-500 mr-1">Summary</span>
                <Highlight text={r.summary_snippet} query={query} />
              </p>
            )}
            {r.segments.length > 0 && (
              <ul className="mt-2 space-y-1">
                {r.segments.map((seg, i) => (
                  <li key={i} className="text-sm text-gray-700 flex gap-2">
                    <span className="text-xs text-gray-400 tabular-nums shrink-0 pt-0.5 w-12">
                      {formatTime(seg.start)}
                    </span>
                    <span><Highlight text={seg.text} query={query} /></span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
