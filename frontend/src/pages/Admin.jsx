import { useEffect, useState } from 'react';
import { cleanupAudio, getUsers, setUserRole, translateAll } from '../api/client';
import { useMe } from '../hooks/useMe';

export default function Admin() {
  const { me, reload } = useMe();
  const [users, setUsers] = useState(null);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getUsers().then(setUsers).catch((e) => setError(e.message));
  }, []);

  const run = async (fn) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await fn();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const changeRole = (username, role) =>
    run(async () => {
      const updated = await setUserRole(username, role);
      setUsers((prev) => prev.map((u) => (u.username === username ? updated : u)));
      if (username === me.username) reload();
    });

  const freeDiskSpace = () => {
    if (!window.confirm(
      "Delete the audio files of every processed episode, everyone's? Transcripts and summaries are kept."
    )) return;
    run(async () => {
      const { episodes, bytes_freed } = await cleanupAudio();
      setNotice(
        episodes === 0
          ? 'No audio left to clean up.'
          : `Removed the audio of ${episodes} episode${episodes === 1 ? '' : 's'}, ` +
            `freeing ${(bytes_freed / 1024 ** 3).toFixed(2)} GB.`
      );
    });
  };

  const translateDigests = () =>
    run(async () => {
      const { queued } = await translateAll();
      setNotice(
        queued === 0
          ? 'Every digest already has a French version.'
          : `Queued ${queued} translation${queued === 1 ? '' : 's'} — track them on the Dashboard.`
      );
    });

  return (
    <div className="max-w-2xl">
      <h1 className="text-2xl font-bold text-gray-900 mb-6">Admin</h1>

      <h2 className="text-lg font-semibold text-gray-800 mb-1">Users</h2>
      <p className="text-sm text-gray-500 mb-3">
        Users appear here after they first sign in. Admins see and edit the shared settings
        and this page.
      </p>
      {!users ? (
        !error && <p className="text-sm text-gray-500">Loading...</p>
      ) : (
        <div className="bg-white rounded-lg border overflow-x-auto mb-8">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-gray-600">
              <tr>
                <th className="text-left px-4 py-2">User</th>
                <th className="text-left px-4 py-2">Since</th>
                <th className="text-left px-4 py-2">Role</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {users.map((u) => (
                <tr key={u.username}>
                  <td className="px-4 py-2">
                    <span className="font-medium text-gray-800">{u.display_name || u.username}</span>
                    <span className="block text-xs text-gray-500">
                      {u.username}{u.email && ` · ${u.email}`}
                    </span>
                  </td>
                  <td className="px-4 py-2 text-gray-500">
                    {u.created_at ? new Date(u.created_at).toLocaleDateString() : '—'}
                  </td>
                  <td className="px-4 py-2">
                    <select
                      value={u.role}
                      disabled={busy}
                      onChange={(e) => changeRole(u.username, e.target.value)}
                      aria-label={`Role of ${u.username}`}
                      className="border rounded-lg px-2 py-1 text-sm"
                    >
                      <option value="user">User</option>
                      <option value="admin">Admin</option>
                    </select>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h2 className="text-lg font-semibold text-gray-800 mb-3">Maintenance</h2>
      <div className="flex flex-wrap gap-3">
        <button
          onClick={freeDiskSpace}
          disabled={busy}
          title="Audio is deleted automatically after each digest; this clears what is left from before."
          className="px-4 py-2 border text-sm rounded-lg hover:bg-gray-50 disabled:opacity-50"
        >
          Free disk space
        </button>
        <button
          onClick={translateDigests}
          disabled={busy}
          title="New digests are made in both languages; this adds French to older ones."
          className="px-4 py-2 border text-sm rounded-lg hover:bg-gray-50 disabled:opacity-50"
        >
          Translate older digests to French
        </button>
      </div>
      {notice && <p className="text-sm text-green-700 mt-3">{notice}</p>}
      {error && <p className="text-sm text-red-600 mt-3">{error}</p>}
    </div>
  );
}
