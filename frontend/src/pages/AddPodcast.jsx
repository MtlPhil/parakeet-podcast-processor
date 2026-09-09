import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { addPodcast } from '../api/client';
import { useJobPoller } from '../hooks/useJobPoller';
import JobProgress from '../components/JobProgress';

export default function AddPodcast() {
  const [url, setUrl] = useState('');
  const [name, setName] = useState('');
  const [category, setCategory] = useState('');
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const { job, isPolling, startPolling } = useJobPoller();
  const navigate = useNavigate();

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);

    try {
      const result = await addPodcast({
        url,
        name: name || undefined,
        category: category || undefined,
      });
      startPolling(result.job_id);
    } catch (e) {
      setError(e.message);
    } finally {
      setSubmitting(false);
    }
  };

  const done = job?.status === 'completed';

  return (
    <div className="max-w-lg">
      <h1 className="text-2xl font-bold text-gray-900 mb-6">Add Podcast</h1>

      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Podcast URL *</label>
          <input
            type="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            required
            placeholder="RSS feed or Apple Podcasts link"
            className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
          <p className="mt-1 text-xs text-gray-500">
            Paste an RSS feed URL or an Apple Podcasts link (e.g. podcasts.apple.com/...)
          </p>
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Name (optional)</label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Auto-detected from feed"
            className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Category (optional)</label>
          <input
            type="text"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            placeholder="e.g. tech, business, ai"
            className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>

        {error && <p className="text-sm text-red-600">{error}</p>}

        <button
          type="submit"
          disabled={submitting || isPolling}
          className="w-full px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm font-medium"
        >
          {submitting ? 'Adding...' : isPolling ? 'Fetching episodes...' : 'Add & Fetch Episodes'}
        </button>
      </form>

      {job && (
        <div className="mt-6">
          <JobProgress job={job} />
          {done && (
            <button
              onClick={() => navigate('/podcasts')}
              className="mt-4 w-full px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 text-sm font-medium"
            >
              View Podcasts
            </button>
          )}
        </div>
      )}
    </div>
  );
}
