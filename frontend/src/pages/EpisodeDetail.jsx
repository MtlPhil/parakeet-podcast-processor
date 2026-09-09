import { useEffect, useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import {
  getEpisode,
  getTranscript,
  getSummary,
  transcribeEpisode,
  digestEpisode,
  runPipeline,
} from '../api/client';
import { useJobPoller } from '../hooks/useJobPoller';
import StatusBadge from '../components/StatusBadge';
import JobProgress from '../components/JobProgress';

const TABS = ['overview', 'transcript', 'summary'];

export default function EpisodeDetail() {
  const { id } = useParams();
  const [episode, setEpisode] = useState(null);
  const [transcript, setTranscript] = useState(null);
  const [summary, setSummary] = useState(null);
  const [tab, setTab] = useState('overview');
  const [error, setError] = useState(null);
  const { job, isPolling, startPolling } = useJobPoller();

  const load = async () => {
    try {
      const ep = await getEpisode(id);
      setEpisode(ep);
      // Load transcript if transcribed
      if (ep.status === 'transcribed' || ep.status === 'processed') {
        getTranscript(id).then(setTranscript).catch(() => {});
      }
      // Load summary if processed
      if (ep.status === 'processed') {
        getSummary(id).then(setSummary).catch(() => {});
      }
    } catch (e) {
      setError(e.message);
    }
  };

  useEffect(() => { load(); }, [id]);

  useEffect(() => {
    if (job?.status === 'completed') load();
  }, [job?.status]);

  const handleAction = async (action) => {
    try {
      let result;
      if (action === 'transcribe') result = await transcribeEpisode(id);
      else if (action === 'digest') result = await digestEpisode(id);
      else if (action === 'pipeline') result = await runPipeline(id);
      if (result?.job_id) startPolling(result.job_id);
    } catch (e) {
      setError(e.message);
    }
  };

  if (error) return <p className="text-red-600">Error: {error}</p>;
  if (!episode) return <p className="text-gray-500">Loading...</p>;

  const steps = [
    { label: 'Downloaded', done: true },
    { label: 'Transcribed', done: ['transcribed', 'processed'].includes(episode.status) },
    { label: 'Processed', done: episode.status === 'processed' },
  ];

  return (
    <div>
      <Link to={`/podcasts/${episode.podcast_id}`} className="text-sm text-blue-600 hover:underline">
        &larr; {episode.podcast_title}
      </Link>
      <h1 className="text-2xl font-bold text-gray-900 mt-2 mb-1">{episode.title}</h1>
      <p className="text-sm text-gray-500 mb-4">
        {episode.date ? new Date(episode.date).toLocaleDateString() : 'Unknown date'}
      </p>

      {/* Pipeline progress */}
      <div className="flex gap-2 mb-6">
        {steps.map(({ label, done }, i) => (
          <div key={label} className="flex items-center gap-2">
            <div className={`w-3 h-3 rounded-full ${done ? 'bg-green-500' : 'bg-gray-300'}`} />
            <span className={`text-sm ${done ? 'text-green-700 font-medium' : 'text-gray-400'}`}>
              {label}
            </span>
            {i < steps.length - 1 && <div className="w-8 h-px bg-gray-300" />}
          </div>
        ))}
      </div>

      {/* Action buttons */}
      <div className="flex gap-3 mb-6">
        {episode.status === 'downloaded' && (
          <>
            <button
              onClick={() => handleAction('transcribe')}
              disabled={isPolling}
              className="px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 disabled:opacity-50"
            >
              Transcribe
            </button>
            <button
              onClick={() => handleAction('pipeline')}
              disabled={isPolling}
              className="px-4 py-2 bg-purple-600 text-white text-sm rounded-lg hover:bg-purple-700 disabled:opacity-50"
            >
              Run Full Pipeline
            </button>
          </>
        )}
        {episode.status === 'transcribed' && (
          <button
            onClick={() => handleAction('digest')}
            disabled={isPolling}
            className="px-4 py-2 bg-green-600 text-white text-sm rounded-lg hover:bg-green-700 disabled:opacity-50"
          >
            Generate Summary
          </button>
        )}
      </div>

      {job && <div className="mb-6"><JobProgress job={job} /></div>}

      {/* Tabs */}
      <div className="border-b mb-4">
        <div className="flex gap-6">
          {TABS.map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`pb-2 text-sm font-medium border-b-2 transition-colors ${
                tab === t
                  ? 'border-blue-500 text-blue-600'
                  : 'border-transparent text-gray-500 hover:text-gray-700'
              }`}
            >
              {t.charAt(0).toUpperCase() + t.slice(1)}
            </button>
          ))}
        </div>
      </div>

      {/* Tab content */}
      {tab === 'overview' && (
        <div className="space-y-2 text-sm text-gray-700">
          <p><span className="font-medium">Status:</span> <StatusBadge status={episode.status} /></p>
          <p><span className="font-medium">Podcast:</span> {episode.podcast_title}</p>
          <p><span className="font-medium">Audio URL:</span>{' '}
            <span className="text-gray-500 break-all">{episode.url}</span>
          </p>
          {episode.file_path && (
            <p><span className="font-medium">Local file:</span>{' '}
              <span className="text-gray-500">{episode.file_path}</span>
            </p>
          )}
        </div>
      )}

      {tab === 'transcript' && (
        <div>
          {!transcript ? (
            <p className="text-gray-500 text-sm">
              {episode.status === 'downloaded'
                ? 'Transcribe the episode first.'
                : 'Loading transcript...'}
            </p>
          ) : (
            <div className="space-y-2 max-h-[600px] overflow-y-auto">
              {transcript.map((seg) => (
                <div key={seg.id} className="flex gap-3 text-sm">
                  <span className="text-gray-400 w-16 shrink-0 text-right tabular-nums">
                    {formatTime(seg.timestamp_start)}
                  </span>
                  <p className="text-gray-700">{seg.text}</p>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {tab === 'summary' && (
        <div>
          {!summary ? (
            <p className="text-gray-500 text-sm">
              {episode.status !== 'processed'
                ? 'Process the episode first.'
                : 'Loading summary...'}
            </p>
          ) : (
            <div className="space-y-4">
              {summary.full_summary && (
                <div>
                  <h3 className="font-medium text-gray-800 mb-1">Summary</h3>
                  <p className="text-sm text-gray-700">{summary.full_summary}</p>
                </div>
              )}
              {summary.key_topics?.length > 0 && (
                <div>
                  <h3 className="font-medium text-gray-800 mb-1">Key Topics</h3>
                  <div className="flex flex-wrap gap-2">
                    {summary.key_topics.map((t, i) => (
                      <span key={i} className="bg-blue-50 text-blue-700 text-xs px-2 py-1 rounded">
                        {t}
                      </span>
                    ))}
                  </div>
                </div>
              )}
              {summary.themes?.length > 0 && (
                <div>
                  <h3 className="font-medium text-gray-800 mb-1">Themes</h3>
                  <div className="flex flex-wrap gap-2">
                    {summary.themes.map((t, i) => (
                      <span key={i} className="bg-purple-50 text-purple-700 text-xs px-2 py-1 rounded">
                        {t}
                      </span>
                    ))}
                  </div>
                </div>
              )}
              {summary.quotes?.length > 0 && (
                <div>
                  <h3 className="font-medium text-gray-800 mb-1">Notable Quotes</h3>
                  {summary.quotes.map((q, i) => (
                    <blockquote
                      key={i}
                      className="border-l-2 border-gray-300 pl-3 text-sm text-gray-600 italic my-2"
                    >
                      {q}
                    </blockquote>
                  ))}
                </div>
              )}
              {summary.startups?.length > 0 && (
                <div>
                  <h3 className="font-medium text-gray-800 mb-1">Companies Mentioned</h3>
                  <div className="flex flex-wrap gap-2">
                    {summary.startups.map((s, i) => (
                      <span key={i} className="bg-green-50 text-green-700 text-xs px-2 py-1 rounded">
                        {s}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function formatTime(seconds) {
  if (seconds == null) return '—';
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}
