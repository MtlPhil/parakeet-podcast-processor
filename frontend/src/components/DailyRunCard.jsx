const STATE_STYLES = {
  running: 'bg-blue-100 text-blue-700',
  finished: 'bg-green-100 text-green-700',
  interrupted: 'bg-red-100 text-red-700',
};

const count = (jobs, type, status) => {
  const byStatus = jobs[type] || {};
  return status ? byStatus[status] || 0 : Object.values(byStatus).reduce((a, b) => a + b, 0);
};

export default function DailyRunCard({ run }) {
  if (!run) return null;
  const { jobs } = run;
  const started = run.started_at ? new Date(run.started_at) : null;

  const lines = [
    `${count(jobs, 'fetch')} source${count(jobs, 'fetch') === 1 ? '' : 's'} checked`,
    `${run.new_episodes} new episode${run.new_episodes === 1 ? '' : 's'}`,
    `${count(jobs, 'full_pipeline', 'completed')} of ${count(jobs, 'full_pipeline')} processed`,
  ];
  if (count(jobs, 'write_linkedin')) {
    lines.push(`${count(jobs, 'write_linkedin', 'completed')} LinkedIn draft(s)`);
  }
  if (run.export_message) lines.push('digest exported');

  return (
    <div className="bg-white rounded-lg border p-4 mb-6">
      <div className="flex flex-wrap items-center gap-2 mb-2">
        <h2 className="text-lg font-semibold text-gray-800">Last daily run</h2>
        <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${STATE_STYLES[run.state] || ''}`}>
          {run.state}
        </span>
        {started && (
          <span className="text-sm text-gray-500">
            {started.toLocaleDateString()} {started.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
          </span>
        )}
      </div>
      <p className="text-sm text-gray-700">{lines.join(' · ')}</p>
      {run.state === 'interrupted' && (
        <p className="text-xs text-gray-500 mt-1">
          The server restarted before the run finished. The next run picks up anything left.
        </p>
      )}
      {run.failures.length > 0 && (
        <details className="mt-2">
          <summary className="text-sm text-red-600 cursor-pointer">
            {run.failures.length} failed job{run.failures.length === 1 ? '' : 's'}
          </summary>
          <ul className="mt-1 space-y-1 text-sm">
            {run.failures.map((f) => (
              <li key={f.job_id} className="text-gray-600">
                <span className="font-medium">{f.job_type}</span>
                {' '}{f.podcast_title}{f.episode_title && ` — ${f.episode_title}`}
                {f.error && <span className="block text-xs text-red-600 break-words">{f.error}</span>}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
