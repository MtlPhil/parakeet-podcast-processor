import { useEffect, useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import { getPodcast, getEpisodes, fetchPodcast } from '../api/client';
import { useJobPoller } from '../hooks/useJobPoller';
import StatusBadge from '../components/StatusBadge';
import JobProgress from '../components/JobProgress';

export default function PodcastDetail() {
  const { id } = useParams();
  const [podcast, setPodcast] = useState(null);
  const [episodes, setEpisodes] = useState([]);
  const [error, setError] = useState(null);
  const { job, isPolling, startPolling } = useJobPoller();

  const load = async () => {
    try {
      const [p, eps] = await Promise.all([
        getPodcast(id),
        getEpisodes({ podcast_id: id }),
      ]);
      setPodcast(p);
      setEpisodes(eps);
    } catch (e) {
      setError(e.message);
    }
  };

  useEffect(() => { load(); }, [id]);

  // Reload episodes when a job finishes
  useEffect(() => {
    if (job?.status === 'completed') load();
  }, [job?.status]);

  const handleFetch = async () => {
    try {
      const result = await fetchPodcast(id);
      startPolling(result.job_id);
    } catch (e) {
      setError(e.message);
    }
  };

  if (error) return <p className="text-red-600">Error: {error}</p>;
  if (!podcast) return <p className="text-gray-500">Loading...</p>;

  return (
    <div>
      <div className="mb-6">
        <Link to="/podcasts" className="text-sm text-blue-600 hover:underline">
          &larr; All Podcasts
        </Link>
        <h1 className="text-2xl font-bold text-gray-900 mt-2">{podcast.title}</h1>
        <p className="text-sm text-gray-500">{podcast.rss_url}</p>
        {podcast.category && (
          <span className="inline-block mt-1 text-xs bg-gray-100 text-gray-600 px-2 py-0.5 rounded">
            {podcast.category}
          </span>
        )}
      </div>

      <div className="flex gap-3 mb-6">
        <button
          onClick={handleFetch}
          disabled={isPolling}
          className="px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 disabled:opacity-50"
        >
          {isPolling ? 'Fetching...' : 'Fetch New Episodes'}
        </button>
      </div>

      {job && <div className="mb-6"><JobProgress job={job} /></div>}

      <h2 className="text-lg font-semibold text-gray-800 mb-3">
        Episodes ({episodes.length})
      </h2>

      {episodes.length === 0 ? (
        <p className="text-gray-500 text-sm">No episodes yet. Click "Fetch New Episodes".</p>
      ) : (
        <div className="bg-white rounded-lg border overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-gray-600">
              <tr>
                <th className="text-left px-4 py-2">Title</th>
                <th className="text-left px-4 py-2">Date</th>
                <th className="text-left px-4 py-2">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {episodes.map((ep) => (
                <tr key={ep.id} className="hover:bg-gray-50">
                  <td className="px-4 py-2">
                    <Link
                      to={`/episodes/${ep.id}`}
                      className="text-blue-600 hover:underline font-medium"
                    >
                      {ep.title}
                    </Link>
                  </td>
                  <td className="px-4 py-2 text-gray-500">
                    {ep.date ? new Date(ep.date).toLocaleDateString() : '—'}
                  </td>
                  <td className="px-4 py-2">
                    <StatusBadge status={ep.status} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
