import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  getStats,
  getJobs,
  processLibrary,
  generateExport,
  retryJob,
  retryFailedJobs,
  clearJobs,
  clearFailedJobs,
} from '../api/client';
import StatusBadge from '../components/StatusBadge';

const today = () => new Date().toISOString().slice(0, 10);
const RETRYABLE = ['fetch', 'transcribe', 'digest', 'full_pipeline'];

const formatStartTime = (ts) => {
  if (!ts) return '—';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
};

const STATUS_FILTERS = [
  { value: 'all', label: 'All statuses' },
  { value: 'active', label: 'Active (pending + running)' },
  { value: 'pending', label: 'Pending' },
  { value: 'running', label: 'Running' },
  { value: 'completed', label: 'Completed' },
  { value: 'failed', label: 'Failed' },
];

const PAGE_SIZE_OPTIONS = [10, 20, 50, 'all'];

// Page numbers to render, with a gap collapsed to a single "…" — keeps the
// pager usable even when there are many pages.
function pageWindow(current, total) {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const pages = [...new Set([1, total, current - 1, current, current + 1])]
    .filter((n) => n >= 1 && n <= total)
    .sort((a, b) => a - b);
  return pages;
}

export default function Dashboard() {
  const [stats, setStats] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState(false);
  const [exportDate, setExportDate] = useState(today());
  const [statusFilter, setStatusFilter] = useState('all');
  const [pageSize, setPageSize] = useState(20);
  const [page, setPage] = useState(1);

  const loadData = async () => {
    try {
      const [s, j] = await Promise.all([getStats(), getJobs()]);
      setStats(s);
      setJobs(j);
    } catch (e) {
      setError(e.message);
    }
  };

  useEffect(() => {
    loadData();
    const timer = setInterval(loadData, 5000);
    return () => clearInterval(timer);
  }, []);

  const runBatch = async (step, count, label) => {
    if (!window.confirm(
      `Queue ${label} for ${count} episode${count === 1 ? '' : 's'}? ` +
      `They run one at a time on the server.`
    )) return;
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      const { queued } = await processLibrary(step);
      setNotice(`Queued ${queued} ${label} job${queued === 1 ? '' : 's'}.`);
      loadData();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const handleRetry = async (jobId) => {
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      await retryJob(jobId);
      setNotice('Job re-queued.');
      loadData();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const handleRetryAll = async () => {
    if (!window.confirm(
      'Re-queue all failed jobs that still need doing, then clear the failed records? ' +
      "Anything already resolved by a later attempt is skipped and cleared too."
    )) return;
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      const { queued } = await retryFailedJobs();
      setNotice(
        queued === 0
          ? 'Nothing to retry — cleared the failed jobs.'
          : `Re-queued ${queued} job${queued === 1 ? '' : 's'} and cleared the failed records.`
      );
      loadData();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const handleClearJobs = async () => {
    if (!window.confirm(
      'Delete all completed/failed job records? Jobs still running are kept. ' +
      "This can't be undone."
    )) return;
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      const { deleted } = await clearJobs();
      setNotice(`Cleared ${deleted} job record${deleted === 1 ? '' : 's'}.`);
      loadData();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const handleClearFailed = async () => {
    if (!window.confirm("Delete all failed job records? This can't be undone.")) return;
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      const { deleted } = await clearFailedJobs();
      setNotice(`Cleared ${deleted} failed job record${deleted === 1 ? '' : 's'}.`);
      loadData();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const runExport = async () => {
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      await generateExport(exportDate);
      setNotice(`Export queued for ${exportDate}.`);
      loadData();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  if (error && !stats) return <p className="text-red-600">Error: {error}</p>;
  if (!stats) return <p className="text-gray-500">Loading...</p>;

  const statCards = [
    { label: 'Podcasts', value: stats.total_podcasts, color: 'bg-purple-50 text-purple-700' },
    { label: 'Episodes', value: stats.total_episodes, color: 'bg-blue-50 text-blue-700' },
    { label: 'Downloaded', value: stats.episodes_downloaded, color: 'bg-yellow-50 text-yellow-700' },
    { label: 'Transcribed', value: stats.episodes_transcribed, color: 'bg-indigo-50 text-indigo-700' },
    { label: 'Processed', value: stats.episodes_processed, color: 'bg-green-50 text-green-700' },
    { label: 'Active Jobs', value: stats.active_jobs, color: 'bg-orange-50 text-orange-700' },
  ];

  const filteredJobs = jobs.filter((j) => {
    if (statusFilter === 'all') return true;
    if (statusFilter === 'active') return j.status === 'pending' || j.status === 'running';
    return j.status === statusFilter;
  });
  const effectivePageSize = pageSize === 'all' ? filteredJobs.length || 1 : pageSize;
  const totalPages = Math.max(1, Math.ceil(filteredJobs.length / effectivePageSize));
  const currentPage = Math.min(page, totalPages);
  const pageJobs = filteredJobs.slice(
    (currentPage - 1) * effectivePageSize,
    currentPage * effectivePageSize
  );

  const changeFilter = (value) => { setStatusFilter(value); setPage(1); };
  const changePageSize = (value) => { setPageSize(value === 'all' ? 'all' : Number(value)); setPage(1); };

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-gray-900">Dashboard</h1>
        <Link
          to="/add"
          className="px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700"
        >
          + Add Podcast
        </Link>
      </div>

      {/* Stats grid */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4 mb-8">
        {statCards.map(({ label, value, color }) => (
          <div key={label} className={`rounded-lg p-4 ${color}`}>
            <p className="text-2xl font-bold">{value}</p>
            <p className="text-sm opacity-75">{label}</p>
          </div>
        ))}
      </div>

      {/* Pipeline actions */}
      <div className="bg-white rounded-lg border p-4 mb-6">
        <h2 className="text-lg font-semibold text-gray-800 mb-3">Run Pipeline</h2>
        <div className="flex flex-wrap items-center gap-3">
          <button
            disabled={busy || stats.episodes_downloaded === 0}
            onClick={() => runBatch('transcribe', stats.episodes_downloaded, 'transcription')}
            className="px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 disabled:opacity-50"
          >
            Transcribe all downloaded ({stats.episodes_downloaded})
          </button>
          <button
            disabled={busy || stats.episodes_transcribed === 0}
            onClick={() => runBatch('digest', stats.episodes_transcribed, 'digest')}
            className="px-4 py-2 bg-green-600 text-white text-sm rounded-lg hover:bg-green-700 disabled:opacity-50"
          >
            Digest all transcribed ({stats.episodes_transcribed})
          </button>
          <span className="hidden sm:block mx-1 h-6 w-px bg-gray-200" />
          <input
            type="date"
            value={exportDate}
            onChange={(e) => setExportDate(e.target.value)}
            className="border rounded-lg px-3 py-2 text-sm"
          />
          <button
            disabled={busy}
            onClick={runExport}
            className="px-4 py-2 border text-sm rounded-lg hover:bg-gray-50 disabled:opacity-50"
          >
            Generate export
          </button>
        </div>
        {notice && <p className="text-sm text-green-700 mt-3">{notice}</p>}
        {error && <p className="text-sm text-red-600 mt-3">{error}</p>}
      </div>

      {/* Recent jobs */}
      <div className="flex flex-wrap items-center justify-between gap-2 mb-1">
        <h2 className="text-lg font-semibold text-gray-800">Recent Jobs</h2>
        <div className="flex flex-wrap gap-2">
          {jobs.some((j) => j.status === 'failed') && (
            <>
              <button
                onClick={handleRetryAll}
                disabled={busy}
                className="px-3 py-1.5 border text-sm rounded-lg hover:bg-gray-50 disabled:opacity-50"
              >
                Retry all failed
              </button>
              <button
                onClick={handleClearFailed}
                disabled={busy}
                className="px-3 py-1.5 border border-red-200 text-red-600 text-sm rounded-lg hover:bg-red-50 disabled:opacity-50"
              >
                Clear failed jobs
              </button>
            </>
          )}
          {jobs.some((j) => j.status === 'completed' || j.status === 'failed') && (
            <button
              onClick={handleClearJobs}
              disabled={busy}
              className="px-3 py-1.5 border border-red-200 text-red-600 text-sm rounded-lg hover:bg-red-50 disabled:opacity-50"
            >
              Clear recent jobs
            </button>
          )}
        </div>
      </div>
      {(stats.running_jobs > 0 || stats.queued_jobs > 0) && (
        <p className="text-sm text-gray-500 mb-3">
          {stats.running_jobs} running &middot; {stats.queued_jobs} queued
        </p>
      )}
      {jobs.length === 0 ? (
        <p className="text-gray-500 text-sm">No jobs yet. Add a podcast to get started.</p>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-3 mb-3">
            <select
              value={statusFilter}
              onChange={(e) => changeFilter(e.target.value)}
              className="border rounded-lg px-2 py-1.5 text-sm"
            >
              {STATUS_FILTERS.map((f) => (
                <option key={f.value} value={f.value}>{f.label}</option>
              ))}
            </select>
            <label className="flex items-center gap-2 text-sm text-gray-600">
              Show
              <select
                value={pageSize}
                onChange={(e) => changePageSize(e.target.value)}
                className="border rounded-lg px-2 py-1.5 text-sm"
              >
                {PAGE_SIZE_OPTIONS.map((n) => (
                  <option key={n} value={n}>{n === 'all' ? 'All' : n}</option>
                ))}
              </select>
            </label>
            <span className="text-sm text-gray-400">
              {filteredJobs.length} job{filteredJobs.length === 1 ? '' : 's'}
            </span>
          </div>

          {filteredJobs.length === 0 ? (
            <p className="text-gray-500 text-sm">No jobs match this filter.</p>
          ) : (
            <div className="bg-white rounded-lg border overflow-x-auto">
              <table className="w-full min-w-[800px] text-sm">
                <thead className="bg-gray-50 text-gray-600">
                  <tr>
                    <th className="text-left px-4 py-2">Type</th>
                    <th className="text-left px-4 py-2">Target</th>
                    <th className="text-left px-4 py-2">Status</th>
                    <th className="text-left px-4 py-2">Started</th>
                    <th className="text-left px-4 py-2">Message</th>
                    <th className="text-left px-4 py-2">Progress</th>
                    <th className="text-left px-4 py-2"></th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {pageJobs.map((job) => (
                    <tr key={job.id} className="hover:bg-gray-50">
                      <td className="px-4 py-2 font-medium">{job.job_type}</td>
                      <td className="px-4 py-2">
                        {job.podcast_title ? (
                          <>
                            <span className="text-gray-700">{job.podcast_title}</span>
                            {job.episode_title && (
                              <span className="block text-xs text-gray-400 truncate max-w-xs">
                                {job.episode_title}
                              </span>
                            )}
                          </>
                        ) : (
                          <span className="text-gray-400">—</span>
                        )}
                      </td>
                      <td className="px-4 py-2"><StatusBadge status={job.status} /></td>
                      <td className="px-4 py-2 text-gray-500 tabular-nums">
                        {formatStartTime(job.started_at)}
                      </td>
                      <td className="px-4 py-2 text-gray-500 truncate max-w-xs">
                        {job.error || job.message || '—'}
                      </td>
                      <td className="px-4 py-2">
                        {job.status === 'running' ? (
                          <div className="w-24 bg-gray-200 rounded-full h-1.5">
                            <div
                              className="bg-blue-500 h-1.5 rounded-full"
                              style={{ width: `${Math.round(job.progress * 100)}%` }}
                            />
                          </div>
                        ) : (
                          <span className="text-gray-400">{Math.round(job.progress * 100)}%</span>
                        )}
                      </td>
                      <td className="px-4 py-2 whitespace-nowrap">
                        {job.status === 'failed' && RETRYABLE.includes(job.job_type) && (
                          <button
                            onClick={() => handleRetry(job.id)}
                            disabled={busy}
                            className="text-blue-600 hover:underline disabled:opacity-50"
                          >
                            Retry
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {totalPages > 1 && (
            <div className="flex items-center gap-1 mt-3">
              <button
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={currentPage === 1}
                className="px-2 py-1 text-sm border rounded-lg hover:bg-gray-50 disabled:opacity-40"
              >
                Prev
              </button>
              {pageWindow(currentPage, totalPages).map((n, i, arr) => (
                <span key={n} className="flex items-center">
                  {i > 0 && n - arr[i - 1] > 1 && <span className="px-1 text-gray-400">…</span>}
                  <button
                    onClick={() => setPage(n)}
                    className={`px-2.5 py-1 text-sm rounded-lg ${
                      n === currentPage ? 'bg-blue-600 text-white' : 'border hover:bg-gray-50'
                    }`}
                  >
                    {n}
                  </button>
                </span>
              ))}
              <button
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                disabled={currentPage === totalPages}
                className="px-2 py-1 text-sm border rounded-lg hover:bg-gray-50 disabled:opacity-40"
              >
                Next
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
