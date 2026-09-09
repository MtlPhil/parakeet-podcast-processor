import StatusBadge from './StatusBadge';

export default function JobProgress({ job }) {
  if (!job) return null;
  const pct = Math.round((job.progress || 0) * 100);

  return (
    <div className="border rounded-lg p-4 bg-white">
      <div className="flex items-center justify-between mb-2">
        <span className="text-sm font-medium text-gray-700">{job.job_type}</span>
        <StatusBadge status={job.status} />
      </div>
      {job.message && (
        <p className="text-sm text-gray-500 mb-2">{job.message}</p>
      )}
      {job.status === 'running' && (
        <div className="w-full bg-gray-200 rounded-full h-2">
          <div
            className="bg-blue-500 h-2 rounded-full transition-all duration-500"
            style={{ width: `${pct}%` }}
          />
        </div>
      )}
      {job.error && (
        <p className="text-sm text-red-600 mt-2">{job.error}</p>
      )}
    </div>
  );
}
