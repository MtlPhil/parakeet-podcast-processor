import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { getPodcasts, deletePodcast } from '../api/client';

export default function PodcastList() {
  const [podcasts, setPodcasts] = useState([]);
  const [error, setError] = useState(null);

  const load = async () => {
    try {
      setPodcasts(await getPodcasts());
    } catch (e) {
      setError(e.message);
    }
  };

  useEffect(() => { load(); }, []);

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
        <div className="grid gap-4">
          {podcasts.map((p) => (
            <div key={p.id} className="bg-white rounded-lg border p-4 flex items-center justify-between">
              <Link to={`/podcasts/${p.id}`} className="flex-1 min-w-0">
                <h3 className="font-medium text-gray-900 truncate">{p.title}</h3>
                <p className="text-sm text-gray-500 truncate">{p.rss_url}</p>
                <div className="flex gap-3 mt-1 text-xs text-gray-400">
                  {p.category && <span className="bg-gray-100 px-2 py-0.5 rounded">{p.category}</span>}
                  <span>{p.episode_count ?? 0} episodes</span>
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
      )}
    </div>
  );
}
