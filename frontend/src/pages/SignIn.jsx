import { useState } from 'react';
import { createFirstAdmin, login } from '../api/client';
import { Field, Message, inputClass, primaryButton } from '../components/Form';

function Card({ title, subtitle, children }) {
  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-100 p-4">
      <div className="w-full max-w-sm bg-white rounded-xl shadow p-6 space-y-4">
        <div>
          <p className="text-xs font-semibold tracking-wide text-blue-600">P3 · Podcast Processor</p>
          <h1 className="text-xl font-bold text-gray-900 mt-1">{title}</h1>
          {subtitle && <p className="text-sm text-gray-500 mt-1">{subtitle}</p>}
        </div>
        {children}
      </div>
    </div>
  );
}

// Sign in to an install with its own accounts.
export function Login({ onSignedIn }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username.trim(), password);
      onSignedIn();
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  };

  return (
    <Card title="Sign in">
      <form onSubmit={submit} className="space-y-4">
        <Field label="Username" htmlFor="username">
          <input id="username" autoFocus autoComplete="username" value={username}
            onChange={(e) => setUsername(e.target.value)} className={inputClass} />
        </Field>
        <Field label="Password" htmlFor="password">
          <input id="password" type="password" autoComplete="current-password" value={password}
            onChange={(e) => setPassword(e.target.value)} className={inputClass} />
        </Field>
        <Message error={error} />
        <button type="submit" disabled={busy || !username || !password} className={`${primaryButton} w-full`}>
          {busy ? 'Signing in...' : 'Sign in'}
        </button>
        <p className="text-xs text-gray-500">Forgot your password? Ask the person who set up P3.</p>
      </form>
    </Card>
  );
}

// First run: create the admin account. Everything else in setup can be
// skipped; this can't, since nothing works without someone signed in.
export function CreateAdmin({ onCreated }) {
  const [form, setForm] = useState({ display_name: '', username: '', password: '', confirm: '' });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const submit = async (e) => {
    e.preventDefault();
    if (form.password !== form.confirm) {
      setError('The two passwords are different.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await createFirstAdmin({
        username: form.username.trim(),
        password: form.password,
        display_name: form.display_name.trim() || null,
      });
      onCreated();
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  };

  return (
    <Card
      title="Welcome! Let's set up P3"
      subtitle="First, create your account. You'll be the administrator: you can add other people later and choose what they can change."
    >
      <form onSubmit={submit} className="space-y-4">
        <Field label="Your name" htmlFor="display_name">
          <input id="display_name" autoFocus value={form.display_name} onChange={set('display_name')}
            className={inputClass} placeholder="e.g. Marie" />
        </Field>
        <Field label="Username" htmlFor="username" help="What you'll type to sign in. Letters and numbers, no spaces.">
          <input id="username" autoComplete="username" value={form.username} onChange={set('username')}
            className={inputClass} />
        </Field>
        <Field label="Password" htmlFor="password" help="At least 8 characters.">
          <input id="password" type="password" autoComplete="new-password" value={form.password}
            onChange={set('password')} className={inputClass} />
        </Field>
        <Field label="Password again" htmlFor="confirm">
          <input id="confirm" type="password" autoComplete="new-password" value={form.confirm}
            onChange={set('confirm')} className={inputClass} />
        </Field>
        <Message error={error} />
        <button type="submit" disabled={busy || !form.username || !form.password}
          className={`${primaryButton} w-full`}>
          {busy ? 'Creating...' : 'Create my account'}
        </button>
      </form>
    </Card>
  );
}
