import { useCallback, useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { getPodcasts, deletePodcast } from '../api/client';
import SourceBadge from '../components/SourceBadge';
import { TagChips } from '../components/Tags';

const byName = (a, b) =>
  a.title.localeCompare(b.title, undefined, { sensitivity: 'base', numeric: true });

// Name A to Z; newest added first; most episodes first. Ties go by name.
const SORTS = {
  name: { label: 'Name', compare: byName },
  added: {
    label: 'Date added',
    compare: (a, b) => (b.added_at || '').localeCompare(a.added_at || '') || byName(a, b),
  },
  episodes: {
    label: 'Episodes',
    compare: (a, b) => (b.episode_count ?? 0) - (a.episode_count ?? 0) || byName(a, b),
  },
};

export default function PodcastList() {
  const [podcasts, setPodcasts] = useState([]);
  const [error, setError] = useState(null);
  // In the address, so a reload or the back button keeps it.
  const [searchParams, setSearchParams] = useSearchParams();
  const sort = SORTS[searchParams.get('sort')] ? searchParams.get('sort') : 'name';
  const sorted = [...podcasts].sort(SORTS[sort].compare);

  const load = useCallback(
    () => getPodcasts().then(setPodcasts).catch((e) => setError(e.message)),
    []
  );

  useEffect(() => { load(); }, [load]);

  const handleDelete = async (id, title) => {
    if (!confirm(`Delete "${title}" and all its episodes?`)) return;
    try {
      await deletePodcast(id);
      load();
    } catch (e) {
      setError(e.message);
    }
  };

  if (error) return <p className="text-red-600">Error: {error}</p>;

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-gray-900">Podcasts</h1>
        <Link
          to="/add"
          className="px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700"
        >
          + Add Podcast
        </Link>
      </div>

      {podcasts.length === 0 ? (
        <p className="text-gray-500">No podcasts added yet.</p>
      ) : (
        <>
        <div className="flex items-center gap-2 mb-3 text-xs text-gray-500">
          <span>Sort by</span>
          <div className="inline-flex rounded-lg border overflow-hidden" role="group" aria-label="Sort by">
            {Object.entries(SORTS).map(([key, { label }]) => (
              <button
                key={key}
                type="button"
                onClick={() => setSearchParams(key === 'name' ? {} : { sort: key }, { replace: true })}
                aria-pressed={sort === key}
                className={`px-2.5 py-1 ${sort === key ? 'bg-blue-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
        <div className="grid gap-4">
          {sorted.map((p) => (
            <div key={p.id} className="bg-white rounded-lg border p-4 flex items-center justify-between">
              <Link to={`/podcasts/${p.id}`} className="flex-1 min-w-0">
                <h3 className="font-medium text-gray-900 truncate">{p.title}</h3>
                <p className="text-sm text-gray-500 truncate">{p.rss_url}</p>
                <div className="flex flex-wrap gap-3 mt-1 text-xs text-gray-400">
                  <SourceBadge type={p.source_type} />
                  <TagChips value={p.category} className="bg-gray-100 px-2 py-0.5 rounded" />
                  <span>{p.episode_count ?? 0} episodes</span>
                  {p.added_at && <span>Added {new Date(p.added_at).toLocaleDateString()}</span>}
                </div>
              </Link>
              <button
                onClick={() => handleDelete(p.id, p.title)}
                className="ml-4 text-red-500 hover:text-red-700 text-sm"
              >
                Delete
              </button>
            </div>
          ))}
        </div>
        </>
      )}
    </div>
  );
}
