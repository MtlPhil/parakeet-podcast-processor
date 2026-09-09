import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { getStats, getJobs } from '../api/client';
import StatusBadge from '../components/StatusBadge';

export default function Dashboard() {
  const [stats, setStats] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [error, setError] = useState(null);

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

  if (error) return <p className="text-red-600">Error: {error}</p>;
  if (!stats) return <p className="text-gray-500">Loading...</p>;

  const statCards = [
    { label: 'Podcasts', value: stats.total_podcasts, color: 'bg-purple-50 text-purple-700' },
    { label: 'Episodes', value: stats.total_episodes, color: 'bg-blue-50 text-blue-700' },
    { label: 'Downloaded', value: stats.episodes_downloaded, color: 'bg-yellow-50 text-yellow-700' },
    { label: 'Transcribed', value: stats.episodes_transcribed, color: 'bg-indigo-50 text-indigo-700' },
    { label: 'Processed', value: stats.episodes_processed, color: 'bg-green-50 text-green-700' },
    { label: 'Active Jobs', value: stats.active_jobs, color: 'bg-orange-50 text-orange-700' },
  ];

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

      {/* Recent jobs */}
      <h2 className="text-lg font-semibold text-gray-800 mb-3">Recent Jobs</h2>
      {jobs.length === 0 ? (
        <p className="text-gray-500 text-sm">No jobs yet. Add a podcast to get started.</p>
      ) : (
        <div className="bg-white rounded-lg border overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-gray-600">
              <tr>
                <th className="text-left px-4 py-2">Type</th>
                <th className="text-left px-4 py-2">Status</th>
                <th className="text-left px-4 py-2">Message</th>
                <th className="text-left px-4 py-2">Progress</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {jobs.slice(0, 15).map((job) => (
                <tr key={job.id} className="hover:bg-gray-50">
                  <td className="px-4 py-2 font-medium">{job.job_type}</td>
                  <td className="px-4 py-2"><StatusBadge status={job.status} /></td>
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
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
