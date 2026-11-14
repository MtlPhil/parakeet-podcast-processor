import { useEffect, useState } from 'react';
import { getSpeakers, renameSpeaker } from '../api/client';

const minutes = (seconds) => `${Math.max(1, Math.round((seconds || 0) / 60))} min`;

// Who speaks in an episode, most talk first. Admins can fix a name (names
// are shared by everyone who has the episode); onRenamed reloads the
// transcript so its headings follow.
export default function SpeakerNames({ episodeId, canEdit, onRenamed }) {
  const [speakers, setSpeakers] = useState([]);
  const [editing, setEditing] = useState(null);
  const [draft, setDraft] = useState('');
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    getSpeakers(episodeId)
      .then((r) => setSpeakers(r.speakers))
      .catch(() => setSpeakers([]));
  }, [episodeId]);

  if (!speakers.length) return null;

  const save = async (label) => {
    setSaving(true);
    setError(null);
    try {
      const r = await renameSpeaker(episodeId, label, draft);
      setSpeakers(r.speakers);
      setEditing(null);
      onRenamed?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="mb-4 text-sm">
      <p className="text-xs font-semibold text-gray-500 mb-1">Speakers</p>
      <ul className="flex flex-wrap gap-2">
        {speakers.map((s) => (
          <li key={s.label} className="flex items-center gap-1 bg-gray-50 border border-gray-200 rounded-lg px-2 py-1">
            {editing === s.label ? (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  save(s.label);
                }}
                className="flex items-center gap-1"
              >
                <input
                  autoFocus
                  value={draft}
                  maxLength={80}
                  onChange={(e) => setDraft(e.target.value)}
                  placeholder="Name (empty: unnamed)"
                  aria-label={`Name for ${s.shown}`}
                  className="border border-gray-300 rounded px-1 py-0.5 text-sm w-44"
                />
                <button type="submit" disabled={saving} className="text-xs text-blue-600 hover:underline">
                  Save
                </button>
                <button type="button" onClick={() => setEditing(null)} className="text-xs text-gray-500 hover:underline">
                  Cancel
                </button>
              </form>
            ) : (
              <>
                <span className="text-gray-800">{s.shown}</span>
                <span className="text-xs text-gray-400">{minutes(s.seconds)}</span>
                {canEdit && (
                  <button
                    onClick={() => {
                      setEditing(s.label);
                      setDraft(s.name || '');
                    }}
                    className="text-xs text-blue-600 hover:underline ml-1"
                  >
                    Rename
                  </button>
                )}
              </>
            )}
          </li>
        ))}
      </ul>
      {error && <p className="text-xs text-red-600 mt-1">{error}</p>}
    </div>
  );
}
