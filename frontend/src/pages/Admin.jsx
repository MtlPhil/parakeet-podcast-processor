import { useEffect, useState } from 'react';
import { cleanupAudio, createUser, getUsers, resetPassword, setUserRole, translateAll } from '../api/client';
import { Field, inputClass, primaryButton } from '../components/Form';
import { useMe } from '../hooks/useMe';

// Admins add people on installs with P3's own accounts.
function AddPerson({ onAdded }) {
  const empty = { display_name: '', username: '', email: '', password: '', role: 'user' };
  const [form, setForm] = useState(empty);
  const [state, setState] = useState({});
  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const submit = async (e) => {
    e.preventDefault();
    setState({ busy: true });
    try {
      const user = await createUser({
        ...form,
        username: form.username.trim(),
        display_name: form.display_name.trim() || null,
        email: form.email.trim() || null,
      });
      onAdded(user);
      setForm(empty);
      setState({ notice: `Added ${user.username}. Give them their username and password; they can change the password in Settings.` });
    } catch (err) {
      setState({ error: err.message });
    }
  };

  return (
    <form onSubmit={submit} className="bg-white rounded-lg border p-4 space-y-3 mb-8">
      <p className="text-sm font-medium text-gray-800">Add a person</p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <Field label="Name" htmlFor="new-name">
          <input id="new-name" value={form.display_name} onChange={set('display_name')} className={inputClass} />
        </Field>
        <Field label="Username" htmlFor="new-username">
          <input id="new-username" value={form.username} onChange={set('username')} className={inputClass} autoComplete="off" />
        </Field>
        <Field label="Email (optional)" htmlFor="new-email">
          <input id="new-email" type="email" value={form.email} onChange={set('email')} className={inputClass} />
        </Field>
        <Field label="Password" htmlFor="new-password" help="At least 8 characters.">
          <input id="new-password" type="text" value={form.password} onChange={set('password')} className={inputClass} autoComplete="off" />
        </Field>
      </div>
      <Field label="Can they change shared settings and manage people?" htmlFor="new-role">
        <select id="new-role" value={form.role} onChange={set('role')} className={inputClass}>
          <option value="user">No: user</option>
          <option value="admin">Yes: admin</option>
        </select>
      </Field>
      {state.error && <p className="text-sm text-red-600">{state.error}</p>}
      {state.notice && <p className="text-sm text-green-700">{state.notice}</p>}
      <button type="submit" disabled={state.busy || !form.username || !form.password} className={primaryButton}>
        Add person
      </button>
    </form>
  );
}

export default function Admin() {
  const { me, reload, authMode } = useMe();
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

  const setPassword = (username) => {
    const password = window.prompt(`New password for ${username} (at least 8 characters):`);
    if (!password) return;
    run(async () => {
      await resetPassword(username, password);
      setNotice(`Password changed for ${username}. They're signed out everywhere.`);
    });
  };

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
        {authMode === 'local'
          ? 'Everyone who can sign in. Admins see and edit the shared settings and this page.'
          : 'Users appear here after they first sign in. Admins see and edit the shared settings and this page.'}
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
                {authMode === 'local' && <th className="px-4 py-2" />}
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
                  {authMode === 'local' && (
                    <td className="px-4 py-2 text-right">
                      <button onClick={() => setPassword(u.username)} disabled={busy}
                        className="text-xs text-blue-600 underline disabled:opacity-50">
                        Set password
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {authMode === 'local' && users && (
        <AddPerson onAdded={(u) => setUsers((prev) => [...prev, u])} />
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
