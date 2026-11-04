import { useState } from 'react';
import { inputClass } from './Form';

// Asks for the word "delete" to be retyped before an account is deleted.
// Phones capitalize the first letter, so case and stray spaces don't count.
export default function DeleteConfirm({ id, label, onConfirm, onCancel }) {
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const confirmed = typed.trim().toLowerCase() === 'delete';

  const submit = async (e) => {
    e.preventDefault();
    if (!confirmed) return;
    setBusy(true);
    setError(null);
    try {
      await onConfirm();
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="space-y-3">
      <div>
        <label htmlFor={id} className="block text-sm font-medium text-gray-700 mb-1">
          Type <span className="font-mono">delete</span> to confirm
        </label>
        <input
          id={id}
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          autoComplete="off"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          className={inputClass}
        />
      </div>
      {error && <p className="text-sm text-red-600">{error}</p>}
      <div className="flex flex-wrap gap-3">
        <button
          type="submit"
          disabled={!confirmed || busy}
          className="px-4 py-2 bg-red-600 text-white text-sm rounded-lg hover:bg-red-700 disabled:opacity-50 font-medium"
        >
          {busy ? 'Deleting...' : label}
        </button>
        {onCancel && (
          <button type="button" onClick={onCancel} disabled={busy} className="px-4 py-2 border text-sm rounded-lg hover:bg-gray-50 disabled:opacity-50">
            Cancel
          </button>
        )}
      </div>
    </form>
  );
}
