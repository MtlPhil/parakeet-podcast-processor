import { useState } from 'react';
import { emailSynopsis } from '../api/client';

// The Contact Picker API: Chrome on Android, and Safari on iOS once its
// "Contact Picker API" feature flag is on. Elsewhere only typing works.
const canPickContacts =
  typeof navigator !== 'undefined' && 'contacts' in navigator && 'ContactsManager' in window;

export default function EmailSynopsis({ episodeId, language }) {
  const [open, setOpen] = useState(false);
  const [to, setTo] = useState('');
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState(null);

  const pickContacts = async () => {
    try {
      const contacts = await navigator.contacts.select(['email'], { multiple: true });
      const picked = contacts.flatMap((c) => c.email?.slice(0, 1) ?? []);
      const current = to.split(',').map((a) => a.trim()).filter(Boolean);
      setTo([...new Set([...current, ...picked])].join(', '));
    } catch {
      // The picker was dismissed.
    }
  };

  const send = async (e) => {
    e.preventDefault();
    setSending(true);
    setResult(null);
    try {
      const { to: sentTo } = await emailSynopsis(episodeId, { to, language });
      setResult({ ok: true, text: `Sent to ${sentTo.join(', ')}.` });
      setTo('');
      setOpen(false);
    } catch (err) {
      setResult({ ok: false, text: err.message });
    } finally {
      setSending(false);
    }
  };

  return (
    <>
      {!open && (
        <button
          onClick={() => { setOpen(true); setResult(null); }}
          className="px-3 py-1 bg-gray-100 text-gray-700 text-xs rounded-lg hover:bg-gray-200"
        >
          Email
        </button>
      )}
      {open && (
        <form onSubmit={send} className="w-full flex flex-wrap items-center gap-2 mt-1">
          <input
            type="email"
            multiple
            autoComplete="email"
            required
            autoFocus
            value={to}
            onChange={(e) => setTo(e.target.value)}
            placeholder="name@example.com, other@example.com"
            className="flex-1 min-w-[16rem] border rounded-lg px-2 py-1 text-sm"
          />
          {canPickContacts && (
            <button
              type="button"
              onClick={pickContacts}
              className="px-3 py-1 bg-gray-100 text-gray-700 text-xs rounded-lg hover:bg-gray-200"
            >
              Contacts
            </button>
          )}
          <button
            type="submit"
            disabled={sending}
            className="px-3 py-1 bg-blue-600 text-white text-xs rounded-lg hover:bg-blue-700 disabled:opacity-50"
          >
            {sending ? 'Sending…' : 'Send'}
          </button>
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="px-3 py-1 text-gray-500 text-xs hover:text-gray-700"
          >
            Cancel
          </button>
        </form>
      )}
      {result && (
        <p className={`w-full text-xs ${result.ok ? 'text-green-700' : 'text-red-600'}`}>
          {result.text}
        </p>
      )}
    </>
  );
}
