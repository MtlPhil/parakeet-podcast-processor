import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { addPodcast, bulkAddPodcasts, previewSource } from '../api/client';
import { useJobPoller } from '../hooks/useJobPoller';
import JobProgress from '../components/JobProgress';
import EpisodePicker from '../components/EpisodePicker';

const DEFAULT_SELECTED = 5;

// The newest `count` episodes not already in the library.
const defaultSelection = (episodes, count) =>
  new Set(episodes.filter((ep) => !ep.in_library).slice(0, count).map((ep) => ep.guid));

// One URL per line; anything after the URL on a line is ignored.
const parseUrls = (text) =>
  [...new Set(text.split('\n').map((line) => line.match(/https?:\/\/\S+/)?.[0]).filter(Boolean))];

// Feed URLs from an OPML file, the subscription export of podcast apps.
const opmlUrls = (xml) =>
  [...new DOMParser().parseFromString(xml, 'text/xml').querySelectorAll('outline[xmlUrl]')]
    .map((el) => el.getAttribute('xmlUrl'));

export default function AddPodcast() {
  const [mode, setMode] = useState('single');
  const [bulkText, setBulkText] = useState('');
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
      setSelected(defaultSelection(result.episodes, DEFAULT_SELECTED));
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

  const handleBulk = async (e) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const result = await bulkAddPodcasts(parseUrls(bulkText), category || undefined);
      startPolling(result.job_id);
    } catch (e) {
      setError(e.message);
    } finally {
      setSubmitting(false);
    }
  };

  const importOpml = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    const urls = opmlUrls(await file.text());
    if (urls.length === 0) {
      setError('No feeds found in that OPML file.');
      return;
    }
    setError(null);
    setBulkText((prev) => [prev.trim(), ...urls].filter(Boolean).join('\n'));
  };

  const done = job?.status === 'completed';

  return (
    <div className="max-w-lg">
      <h1 className="text-2xl font-bold text-gray-900 mb-6">Add Podcast</h1>

      {!preview && !job && (
        <div className="flex gap-1 mb-4 border-b">
          {[['single', 'One podcast'], ['bulk', 'Bulk add']].map(([value, label]) => (
            <button
              key={value}
              type="button"
              onClick={() => { setMode(value); setError(null); }}
              className={`px-3 py-2 text-sm -mb-px border-b-2 ${
                mode === value ? 'border-blue-600 text-blue-700 font-medium' : 'border-transparent text-gray-500 hover:text-gray-700'
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      )}

      {mode === 'bulk' && !job && (
        <form onSubmit={handleBulk} className="space-y-4">
          <div>
            <label htmlFor="bulk-urls" className="block text-sm font-medium text-gray-700 mb-1">
              URLs, one per line
            </label>
            <textarea
              id="bulk-urls"
              value={bulkText}
              onChange={(e) => setBulkText(e.target.value)}
              rows={10}
              placeholder={'https://example.com/feed.rss\nhttps://podcasts.apple.com/...\nhttps://www.youtube.com/@channel'}
              className="w-full border rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <p className="mt-1 text-xs text-gray-500">
              Same kinds of links as a single add. Each new podcast fetches its newest
              episodes per your settings; podcasts already in the library are skipped.
              {' '}{parseUrls(bulkText).length} URL{parseUrls(bulkText).length === 1 ? '' : 's'} found.
            </p>
            <label className="inline-block mt-2 text-sm text-blue-600 hover:underline cursor-pointer">
              Import an OPML file
              <input type="file" accept=".opml,.xml,text/xml" onChange={importOpml} className="hidden" />
            </label>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Category (optional)</label>
            <input
              type="text"
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              placeholder="Applied to every podcast added"
              className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>

          {error && <p className="text-sm text-red-600">{error}</p>}

          <button
            type="submit"
            disabled={submitting || parseUrls(bulkText).length === 0}
            className="w-full px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm font-medium"
          >
            {submitting ? 'Adding...' : `Add ${parseUrls(bulkText).length} Podcast${parseUrls(bulkText).length === 1 ? '' : 's'}`}
          </button>
        </form>
      )}

      {mode === 'single' && !preview && !job && (
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
            <EpisodePicker
              episodes={preview.episodes}
              selected={selected}
              onChange={setSelected}
              emptyText="No episodes found in the last 12 months."
            />
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
          {mode === 'bulk'
            ? 'Adding podcasts...'
            : isPlaylist ? 'Importing playlist...' : 'Fetching episodes...'}
        </p>
      )}
    </div>
  );
}
