import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { addPodcast, previewSource } from '../api/client';
import { useJobPoller } from '../hooks/useJobPoller';
import JobProgress from '../components/JobProgress';

const DEFAULT_SELECTED = 5;

export default function AddPodcast() {
  const [url, setUrl] = useState('');
  const [name, setName] = useState('');
  const [category, setCategory] = useState('');
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [isPlaylist, setIsPlaylist] = useState(false);
  const [preview, setPreview] = useState(null);
  const [selected, setSelected] = useState(new Set());
  const { job, isPolling, startPolling } = useJobPoller();
  const navigate = useNavigate();

  const startFetch = async (body) => {
    const result = await addPodcast(body);
    setIsPlaylist(result.podcast_id == null);
    startPolling(result.job_id);
  };

  const handlePreview = async (e) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);

    try {
      const result = await previewSource(url);
      setPreview(result);
      setSelected(new Set(result.episodes.slice(0, DEFAULT_SELECTED).map((ep) => ep.guid)));
    } catch (e) {
      // Playlists can't be previewed (every video is imported automatically);
      // fall back to the direct add-and-fetch flow instead of erroring out.
      if (/playlist/i.test(e.message)) {
        try {
          await startFetch({ url, name: name || undefined, category: category || undefined });
        } catch (e2) {
          setError(e2.message);
        }
      } else {
        setError(e.message);
      }
    } finally {
      setSubmitting(false);
    }
  };

  const handleConfirm = async (e) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);

    try {
      await startFetch({
        url: preview.url,
        name: name || preview.name || undefined,
        category: category || undefined,
        episode_guids: Array.from(selected),
      });
    } catch (e) {
      setError(e.message);
    } finally {
      setSubmitting(false);
    }
  };

  const toggle = (guid) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(guid)) next.delete(guid);
      else next.add(guid);
      return next;
    });
  };

  const done = job?.status === 'completed';

  return (
    <div className="max-w-lg">
      <h1 className="text-2xl font-bold text-gray-900 mb-6">Add Podcast</h1>

      {!preview && !job && (
        <form onSubmit={handlePreview} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">URL *</label>
            <input
              type="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              required
              placeholder="RSS feed, Apple Podcasts or YouTube link"
              className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <p className="mt-1 text-xs text-gray-500">
              Paste an RSS feed URL, an Apple Podcasts link, or a YouTube channel, video or
              playlist URL. You'll be able to pick which episodes to fetch first (YouTube
              channels list uploads from the last 12 months). Each video of a playlist is
              added as its own source automatically. YouTube Shorts and livestreams are
              skipped.
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
            disabled={submitting}
            className="w-full px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm font-medium"
          >
            {submitting ? 'Looking up episodes...' : 'Continue'}
          </button>
        </form>
      )}

      {preview && !job && (
        <form onSubmit={handleConfirm} className="space-y-4">
          <div>
            <p className="text-sm text-gray-700">
              <span className="font-medium">{preview.name || preview.url}</span> has{' '}
              {preview.episodes.length} available episode{preview.episodes.length === 1 ? '' : 's'}.
              Pick which ones to fetch now &mdash; every future fetch will pull the top episodes
              per your settings.
            </p>
            <div className="mt-2 flex gap-3 text-xs">
              <button
                type="button"
                onClick={() => setSelected(new Set(preview.episodes.map((ep) => ep.guid)))}
                className="text-blue-600 hover:underline"
              >
                Select all
              </button>
              <button
                type="button"
                onClick={() => setSelected(new Set())}
                className="text-blue-600 hover:underline"
              >
                Select none
              </button>
            </div>
          </div>

          <div className="border rounded-lg divide-y max-h-96 overflow-y-auto">
            {preview.episodes.map((ep) => (
              <label
                key={ep.guid}
                className="flex items-start gap-3 px-3 py-2 text-sm hover:bg-gray-50 cursor-pointer"
              >
                <input
                  type="checkbox"
                  checked={selected.has(ep.guid)}
                  onChange={() => toggle(ep.guid)}
                  className="mt-1"
                />
                <span>
                  <span className="block text-gray-900">{ep.title}</span>
                  {ep.date && (
                    <span className="block text-xs text-gray-500">
                      {new Date(ep.date).toLocaleDateString()}
                    </span>
                  )}
                </span>
              </label>
            ))}
            {preview.episodes.length === 0 && (
              <p className="px-3 py-4 text-sm text-gray-500">
                No episodes found in the last 12 months.
              </p>
            )}
          </div>

          {error && <p className="text-sm text-red-600">{error}</p>}

          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => setPreview(null)}
              className="px-4 py-2 border rounded-lg hover:bg-gray-50 text-sm font-medium"
            >
              Back
            </button>
            <button
              type="submit"
              disabled={submitting || selected.size === 0}
              className="flex-1 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm font-medium"
            >
              {submitting
                ? 'Adding...'
                : `Add & Fetch ${selected.size} Episode${selected.size === 1 ? '' : 's'}`}
            </button>
          </div>
        </form>
      )}

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
      {isPolling && !job && (
        <p className="mt-6 text-sm text-gray-500">
          {isPlaylist ? 'Importing playlist...' : 'Fetching episodes...'}
        </p>
      )}
    </div>
  );
}
