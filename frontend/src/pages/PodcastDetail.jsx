import { useEffect, useState } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import {
  getPodcast,
  getEpisodes,
  fetchPodcast,
  updatePodcast,
  deletePodcast,
  processPodcast,
  transcribeEpisode,
  digestEpisode,
  exportPodcastTranscripts,
} from '../api/client';
import { useJobPoller } from '../hooks/useJobPoller';
import StatusBadge from '../components/StatusBadge';
import JobProgress from '../components/JobProgress';

export default function PodcastDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [podcast, setPodcast] = useState(null);
  const [episodes, setEpisodes] = useState([]);
  const [error, setError] = useState(null);
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState({ title: '', category: '' });
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState(false);
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

  const startEdit = () => {
    setForm({
      title: podcast.title || '',
      category: podcast.category || '',
    });
    setError(null);
    setEditing(true);
  };

  const handleSave = async (e) => {
    e.preventDefault();
    setSaving(true);
    try {
      const updated = await updatePodcast(id, {
        title: form.title.trim(),
        category: form.category.trim(),
      });
      setPodcast(updated);
      setEditing(false);
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!confirm(`Delete "${podcast.title}" and all its episodes?`)) return;
    try {
      await deletePodcast(id);
      navigate('/podcasts');
    } catch (err) {
      setError(err.message);
    }
  };

  const eligible = {
    transcribe: episodes.filter((e) => e.status === 'downloaded').length,
    digest: episodes.filter((e) => e.status === 'transcribed').length,
    pipeline: episodes.filter((e) => e.status !== 'processed').length,
    transcripts: episodes.filter((e) => ['transcribed', 'processed'].includes(e.status)).length,
  };

  const handleExportTranscripts = async () => {
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      await exportPodcastTranscripts(id);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const runBulk = async (step) => {
    const n = eligible[step];
    if (!n) return;
    if (!window.confirm(
      `Queue ${step} for ${n} episode${n === 1 ? '' : 's'} of "${podcast.title}"? ` +
      `They run one at a time on the server.`
    )) return;
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      const { queued } = await processPodcast(id, step);
      setNotice(`Queued ${queued} ${step} job${queued === 1 ? '' : 's'} — track progress on the Dashboard.`);
      load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const runEpisode = async (ep, step) => {
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      if (step === 'transcribe') await transcribeEpisode(ep.id);
      else await digestEpisode(ep.id);
      setNotice(`Queued ${step} for "${ep.title}".`);
      load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  if (error && !podcast) return <p className="text-red-600">Error: {error}</p>;
  if (!podcast) return <p className="text-gray-500">Loading...</p>;

  return (
    <div>
      <div className="mb-6">
        <Link to="/podcasts" className="text-sm text-blue-600 hover:underline">
          &larr; All Podcasts
        </Link>

        {editing ? (
          <form onSubmit={handleSave} className="mt-3 space-y-3 max-w-lg">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Title</label>
              <input
                value={form.title}
                onChange={(e) => setForm({ ...form, title: e.target.value })}
                required
                className="w-full border rounded-lg px-3 py-2 text-sm"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Category</label>
              <input
                value={form.category}
                onChange={(e) => setForm({ ...form, category: e.target.value })}
                className="w-full border rounded-lg px-3 py-2 text-sm"
              />
            </div>
            {error && <p className="text-sm text-red-600">{error}</p>}
            <div className="flex gap-2">
              <button
                type="submit"
                disabled={saving}
                className="px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 disabled:opacity-50"
              >
                {saving ? 'Saving...' : 'Save'}
              </button>
              <button
                type="button"
                onClick={() => { setEditing(false); setError(null); }}
                className="px-4 py-2 border text-sm rounded-lg hover:bg-gray-50"
              >
                Cancel
              </button>
            </div>
          </form>
        ) : (
          <>
            <h1 className="text-2xl font-bold text-gray-900 mt-2">{podcast.title}</h1>
            <p className="text-sm text-gray-500 break-all">{podcast.rss_url}</p>
            {podcast.category && (
              <span className="inline-block mt-1 text-xs bg-gray-100 text-gray-600 px-2 py-0.5 rounded">
                {podcast.category}
              </span>
            )}
          </>
        )}
      </div>

      {!editing && (
        <>
          <div className="flex flex-wrap gap-3 mb-3">
            <button
              onClick={handleFetch}
              disabled={isPolling}
              className="px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 disabled:opacity-50"
            >
              {isPolling ? 'Fetching...' : 'Fetch New Episodes'}
            </button>
            <button
              onClick={startEdit}
              className="px-4 py-2 border text-sm rounded-lg hover:bg-gray-50"
            >
              Edit
            </button>
            <button
              onClick={handleDelete}
              className="px-4 py-2 border border-red-200 text-red-600 text-sm rounded-lg hover:bg-red-50"
            >
              Delete
            </button>
          </div>
          <div className="flex flex-wrap gap-3 mb-6">
            <button
              onClick={() => runBulk('transcribe')}
              disabled={busy || eligible.transcribe === 0}
              className="px-4 py-2 border text-sm rounded-lg hover:bg-gray-50 disabled:opacity-50"
            >
              Transcribe all ({eligible.transcribe})
            </button>
            <button
              onClick={() => runBulk('digest')}
              disabled={busy || eligible.digest === 0}
              className="px-4 py-2 border text-sm rounded-lg hover:bg-gray-50 disabled:opacity-50"
            >
              Digest all ({eligible.digest})
            </button>
            <button
              onClick={() => runBulk('pipeline')}
              disabled={busy || eligible.pipeline === 0}
              className="px-4 py-2 border text-sm rounded-lg hover:bg-gray-50 disabled:opacity-50"
            >
              Run full pipeline ({eligible.pipeline})
            </button>
            <button
              onClick={handleExportTranscripts}
              disabled={busy || eligible.transcripts === 0}
              className="px-4 py-2 border text-sm rounded-lg hover:bg-gray-50 disabled:opacity-50"
            >
              Export all transcripts ({eligible.transcripts})
            </button>
          </div>
        </>
      )}

      {notice && <p className="text-sm text-green-700 mb-4">{notice}</p>}
      {error && !editing && <p className="text-sm text-red-600 mb-4">{error}</p>}

      {job && <div className="mb-6"><JobProgress job={job} /></div>}

      <h2 className="text-lg font-semibold text-gray-800 mb-3">
        Episodes ({episodes.length})
      </h2>

      {episodes.length === 0 ? (
        <p className="text-gray-500 text-sm">No episodes yet. Click "Fetch New Episodes".</p>
      ) : (
        <div className="bg-white rounded-lg border overflow-x-auto">
          <table className="w-full min-w-[560px] text-sm">
            <thead className="bg-gray-50 text-gray-600">
              <tr>
                <th className="text-left px-4 py-2">Title</th>
                <th className="text-left px-4 py-2">Date</th>
                <th className="text-left px-4 py-2">Status</th>
                <th className="text-left px-4 py-2">Actions</th>
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
                  <td className="px-4 py-2">
                    {ep.status === 'downloaded' && (
                      <button
                        onClick={() => runEpisode(ep, 'transcribe')}
                        disabled={busy}
                        className="text-blue-600 hover:underline disabled:opacity-50"
                      >
                        Transcribe
                      </button>
                    )}
                    {ep.status === 'transcribed' && (
                      <button
                        onClick={() => runEpisode(ep, 'digest')}
                        disabled={busy}
                        className="text-green-600 hover:underline disabled:opacity-50"
                      >
                        Digest
                      </button>
                    )}
                    {ep.status === 'processed' && (
                      <span className="text-gray-400">—</span>
                    )}
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
