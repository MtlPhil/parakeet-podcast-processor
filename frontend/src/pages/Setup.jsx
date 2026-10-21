import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  checkOllama,
  finishSetup,
  getAuthStatus,
  getNetwork,
  getSecrets,
  getSettings,
  sendTestEmail,
  setNetwork,
  setSecrets,
  updateMySettings,
  updateSettings,
} from '../api/client';
import { useMe } from '../hooks/useMe';
import AddPodcast from './AddPodcast';
import {
  Checkbox,
  Choice,
  Field,
  Message,
  inputClass,
  primaryButton,
  secondaryButton,
} from '../components/Form';

// The setup wizard. Admins see every page; other users only their own
// (language, daily digest, podcasts). Every page can be skipped, and the
// wizard stays available from Settings.

async function saveShared(changes) {
  const { settings } = await getSettings();
  await updateSettings({ settings: { ...settings, ...changes } });
}

async function saveMine(me, changes) {
  await updateMySettings({ ...me.own_settings, ...changes });
}

function StepFrame({ title, intro, children, onSave, saveLabel = 'Save and continue', skipLabel = 'Skip this step', onSkip, onBack, canSave = true }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await onSave();
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  };

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-xl font-bold text-gray-900">{title}</h2>
        {intro && <p className="text-sm text-gray-600 mt-1">{intro}</p>}
      </div>
      {children}
      <Message error={error} />
      <div className="flex flex-wrap items-center gap-3 pt-2 border-t">
        {onBack && (
          <button type="button" onClick={onBack} disabled={busy} className="text-sm text-gray-500 hover:text-gray-800 mr-auto">
            ← Back
          </button>
        )}
        <button type="button" onClick={onSkip} disabled={busy} className={`${secondaryButton} ${onBack ? '' : 'ml-auto'}`}>
          {skipLabel}
        </button>
        {onSave && (
          <button type="button" onClick={save} disabled={busy || !canSave} className={primaryButton}>
            {busy ? 'Saving...' : saveLabel}
          </button>
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------- language

function LanguageStep({ me, reload, ...nav }) {
  const [language, setLanguage] = useState(me.settings.language || 'en');
  return (
    <StepFrame
      {...nav}
      title="Language"
      intro="P3 writes every summary in English and French. Which should it show you first?"
      onSave={async () => {
        await saveMine(me, { language });
        await reload();
        nav.onNext();
      }}
    >
      <div className="space-y-2">
        <Choice name="language" value="en" current={language} onChange={setLanguage} title="English" />
        <Choice name="language" value="fr" current={language} onChange={setLanguage} title="Français (Québec)" />
      </div>
    </StepFrame>
  );
}

// ------------------------------------------------------------ summaries

function SummariesStep(nav) {
  const [provider, setProvider] = useState(null);
  const [model, setModel] = useState('');
  const [keys, setKeys] = useState({ gemini_api_key: '', openai_api_key: '' });
  const [saved, setSaved] = useState(null);
  const [ollama, setOllama] = useState(null);

  const probeOllama = () => {
    setOllama({ checking: true });
    checkOllama().then(setOllama).catch(() => setOllama({ reachable: false }));
  };

  useEffect(() => {
    getSettings().then(({ settings }) => {
      setProvider(settings.llm_provider || 'gemini');
      setModel(settings.llm_model || '');
      if (settings.llm_provider === 'ollama') {
        checkOllama().then(setOllama).catch(() => setOllama({ reachable: false }));
      }
    });
    getSecrets().then(setSaved);
  }, []);

  const choose = (value) => {
    setProvider(value);
    if (value === 'ollama' && !ollama) probeOllama();
  };

  const keyField = (name, label, help) => (
    <Field label={label} htmlFor={name} help={help}>
      <input
        id={name}
        type="password"
        autoComplete="off"
        value={keys[name]}
        placeholder={saved?.[name] ? 'A key is saved. Type a new one to replace it.' : 'Paste your key here'}
        onChange={(e) => setKeys((k) => ({ ...k, [name]: e.target.value }))}
        className={inputClass}
      />
    </Field>
  );

  const save = async () => {
    const changes = { llm_provider: provider };
    if (provider === 'openai' || provider === 'ollama') changes.llm_model = model.trim();
    if (provider === 'openai' && !changes.llm_model) changes.llm_model = 'gpt-4o-mini';
    if (provider === 'ollama' && !changes.llm_model) throw new Error('Pick a model for Ollama.');
    await saveShared(changes);
    const secretChanges = {};
    if (keys.gemini_api_key.trim()) secretChanges.gemini_api_key = keys.gemini_api_key;
    if (keys.openai_api_key.trim()) secretChanges.openai_api_key = keys.openai_api_key;
    if (Object.keys(secretChanges).length) await setSecrets(secretChanges);
    nav.onNext();
  };

  if (!provider) return <p className="text-sm text-gray-500">Loading...</p>;

  return (
    <StepFrame
      {...nav}
      title="Summaries"
      intro="After transcribing an episode, P3 asks an AI service to summarize it. Choose which one. You can change this later in Settings."
      skipLabel="Decide later"
      onSave={save}
    >
      <div className="space-y-2">
        <Choice name="provider" value="gemini" current={provider} onChange={choose} title="Google Gemini (recommended)">
          Good summaries at little or no cost. Needs an API key: sign in at{' '}
          <a href="https://aistudio.google.com/apikey" target="_blank" rel="noreferrer" className="text-blue-600 underline">aistudio.google.com/apikey</a>
          , click “Create API key”, and paste it below.
        </Choice>
        <Choice name="provider" value="openai" current={provider} onChange={choose} title="OpenAI (ChatGPT)">
          Needs an API key from platform.openai.com. Usage is billed by OpenAI.
        </Choice>
        <Choice name="provider" value="ollama" current={provider} onChange={choose} title="Ollama, on this Mac">
          Free and private: nothing leaves this Mac. Needs the Ollama app and a fast Mac with plenty of memory; summaries are slower.
        </Choice>
      </div>

      {provider === 'gemini' && keyField('gemini_api_key', 'Gemini API key')}
      {provider === 'openai' && (
        <>
          {keyField('openai_api_key', 'OpenAI API key')}
          <Field label="Model" htmlFor="model" help="Leave empty for gpt-4o-mini.">
            <input id="model" value={model} onChange={(e) => setModel(e.target.value)} className={inputClass} />
          </Field>
        </>
      )}
      {provider === 'ollama' && (
        <div className="space-y-3">
          {!ollama || ollama.checking ? (
            <p className="text-sm text-gray-500">Looking for Ollama...</p>
          ) : ollama.reachable ? (
            ollama.models.length ? (
              <Field label="Model" htmlFor="ollama-model">
                <select id="ollama-model" value={model} onChange={(e) => setModel(e.target.value)} className={inputClass}>
                  <option value="">Choose a model</option>
                  {ollama.models.map((m) => <option key={m} value={m}>{m}</option>)}
                </select>
              </Field>
            ) : (
              <p className="text-sm text-amber-700">
                Ollama is running but has no models yet. Open Ollama, download a model (for example
                “llama3.2”), then check again.
              </p>
            )
          ) : (
            <p className="text-sm text-amber-700">
              Ollama isn't running on this Mac. Install it from{' '}
              <a href="https://ollama.com/download" target="_blank" rel="noreferrer" className="underline">ollama.com/download</a>
              , open it, download a model, then check again.
            </p>
          )}
          <button type="button" onClick={probeOllama} className={secondaryButton}>Check again</button>
        </div>
      )}
    </StepFrame>
  );
}

// -------------------------------------------------------- transcription

function TranscriptionStep(nav) {
  const [settings, setSettings] = useState(null);
  useEffect(() => { getSettings().then(({ settings: s }) => setSettings(s)); }, []);
  const set = (key, value) => setSettings((s) => ({ ...s, [key]: value }));

  if (!settings) return <p className="text-sm text-gray-500">Loading...</p>;
  return (
    <StepFrame
      {...nav}
      title="Transcription"
      intro="P3 turns each episode's audio into text on this Mac. The defaults suit most people."
      skipLabel="Keep the defaults"
      onSave={async () => {
        await saveShared({
          parakeet_enabled: !!settings.parakeet_enabled,
          whisper_model: settings.whisper_model || 'turbo',
          max_episodes_per_feed: Number(settings.max_episodes_per_feed) || 3,
        });
        nav.onNext();
      }}
    >
      <Checkbox id="parakeet" checked={settings.parakeet_enabled} onChange={(v) => set('parakeet_enabled', v)}>
        <span className="font-medium">Use Parakeet when it can</span> (recommended): much faster for
        the languages it supports. Other languages use Whisper.
      </Checkbox>
      <Field label="Whisper quality" htmlFor="whisper">
        <select id="whisper" value={settings.whisper_model || 'turbo'} onChange={(e) => set('whisper_model', e.target.value)} className={inputClass}>
          <option value="turbo">Balanced (recommended)</option>
          <option value="small">Lighter: faster, less accurate, for Macs with less memory</option>
          <option value="large">Best: slowest, most accurate</option>
        </select>
      </Field>
      <Field label="Episodes per podcast" htmlFor="max-eps"
        help="How many of a podcast's latest episodes P3 gets when you add it and each day after.">
        <input id="max-eps" type="number" min="1" max="100" value={settings.max_episodes_per_feed ?? 3}
          onChange={(e) => set('max_episodes_per_feed', e.target.value)} className={inputClass} />
      </Field>
      <p className="text-xs text-gray-500">
        The first transcription downloads the speech models, which takes a while. After that it's quicker.
      </p>
    </StepFrame>
  );
}

// ------------------------------------------------------- daily + email

function DailyStep({ me, reload, ...nav }) {
  const isAdmin = me.role === 'admin';
  const own = me.settings;
  const [daily, setDaily] = useState(own.daily_run_time !== '');
  const [time, setTime] = useState(own.daily_run_time || '04:00');
  const [emailMe, setEmailMe] = useState(!!own.daily_run_email);
  const [email, setEmail] = useState(own.email || me.email || '');
  const [smtp, setSmtp] = useState(null);
  const [password, setPassword] = useState('');
  const [secrets, setSecretsState] = useState(null);
  const [test, setTest] = useState({});

  useEffect(() => {
    if (!isAdmin) return;
    getSettings().then(({ settings }) =>
      setSmtp({
        smtp_host: settings.smtp_host || '',
        smtp_port: settings.smtp_port || 587,
        smtp_user: settings.smtp_user || '',
        smtp_from: settings.smtp_from || '',
      })
    );
    getSecrets().then(setSecretsState);
  }, [isAdmin]);

  const setS = (key) => (e) => setSmtp((s) => ({ ...s, [key]: e.target.value }));
  const useGmail = () =>
    setSmtp((s) => ({ ...s, smtp_host: 'smtp.gmail.com', smtp_port: 587, smtp_user: s.smtp_user || email, smtp_from: s.smtp_from || email }));

  const persist = async () => {
    if (emailMe && !email.trim()) throw new Error('Enter the address the digest should go to.');
    await saveMine(me, { daily_run_time: daily ? time : '', daily_run_email: emailMe, email: email.trim() });
    if (isAdmin && smtp) {
      await saveShared({ ...smtp, smtp_port: Number(smtp.smtp_port) || 587 });
      if (password.trim()) await setSecrets({ smtp_password: password });
    }
    await reload();
  };

  const sendTest = async () => {
    setTest({ busy: true });
    try {
      await persist();
      const { to } = await sendTestEmail(email.trim());
      setTest({ notice: `Sent to ${to}. Check your inbox (and spam folder).` });
    } catch (e) {
      setTest({ error: e.message });
    }
  };

  return (
    <StepFrame
      {...nav}
      title="Daily digest"
      intro="Once a day, P3 can check your podcasts for new episodes, process them, and email you a summary."
      onSave={async () => { await persist(); nav.onNext(); }}
    >
      <Checkbox id="daily" checked={daily} onChange={setDaily}>Check for new episodes every day</Checkbox>
      {daily && (
        <Field label="At" htmlFor="time" help="The Mac must be on (asleep is fine; P3 catches up when it wakes).">
          <input id="time" type="time" value={time} onChange={(e) => setTime(e.target.value)} className={`${inputClass} max-w-[10rem]`} />
        </Field>
      )}
      <Checkbox id="email-me" checked={emailMe} onChange={setEmailMe}>Email me a digest after each daily check</Checkbox>
      {(emailMe || isAdmin) && (
        <Field label="Your email address" htmlFor="email">
          <input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} className={inputClass} />
        </Field>
      )}

      {isAdmin && smtp && (
        <div className="border rounded-lg p-4 space-y-3 bg-gray-50">
          <div className="flex items-center justify-between gap-3">
            <p className="text-sm font-medium text-gray-800">How P3 sends email (for everyone)</p>
            <button type="button" onClick={useGmail} className="text-xs text-blue-600 underline">I use Gmail</button>
          </div>
          <p className="text-xs text-gray-600">
            P3 sends through your own email account. With Gmail, use an{' '}
            <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noreferrer" className="underline">app password</a>
            {' '}(it needs 2-step verification), not your normal password.
          </p>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <div className="sm:col-span-2">
              <Field label="Mail server" htmlFor="smtp-host">
                <input id="smtp-host" value={smtp.smtp_host} onChange={setS('smtp_host')} className={inputClass} placeholder="smtp.gmail.com" />
              </Field>
            </div>
            <Field label="Port" htmlFor="smtp-port">
              <input id="smtp-port" type="number" value={smtp.smtp_port} onChange={setS('smtp_port')} className={inputClass} />
            </Field>
          </div>
          <Field label="Account (usually your email address)" htmlFor="smtp-user">
            <input id="smtp-user" value={smtp.smtp_user} onChange={setS('smtp_user')} className={inputClass} />
          </Field>
          <Field label="Password" htmlFor="smtp-pass">
            <input id="smtp-pass" type="password" autoComplete="off" value={password} onChange={(e) => setPassword(e.target.value)}
              className={inputClass} placeholder={secrets?.smtp_password ? 'A password is saved. Type a new one to replace it.' : ''} />
          </Field>
        </div>
      )}
      {!isAdmin && emailMe && (
        <p className="text-xs text-gray-500">The person who set up P3 chooses how it sends email.</p>
      )}

      {(emailMe || isAdmin) && (
        <div className="space-y-2">
          <button type="button" onClick={sendTest} disabled={test.busy || !email.trim()} className={secondaryButton}>
            {test.busy ? 'Sending...' : 'Save and send a test email'}
          </button>
          <Message error={test.error} notice={test.notice} />
        </div>
      )}
    </StepFrame>
  );
}

// -------------------------------------------------------------- network

async function waitForServer() {
  await new Promise((r) => setTimeout(r, 2000));
  for (let i = 0; i < 40; i++) {
    try {
      await getAuthStatus();
      return;
    } catch {
      await new Promise((r) => setTimeout(r, 1000));
    }
  }
  throw new Error('P3 did not come back after restarting. Wait a minute and reload this page.');
}

function NetworkStep(nav) {
  const [net, setNet] = useState(null);
  const [lan, setLan] = useState(false);
  const [busyJobs, setBusyJobs] = useState(null);
  const [restarting, setRestarting] = useState(false);

  useEffect(() => {
    getNetwork().then((n) => { setNet(n); setLan(n.lan); });
  }, []);

  const apply = async (force = false) => {
    if (lan === net.lan) return nav.onNext();
    try {
      const updated = await setNetwork(lan, force);
      setBusyJobs(null);
      if (updated.managed) {
        setRestarting(true);
        await waitForServer();
        setRestarting(false);
      }
      setNet(updated);
      nav.onNext();
    } catch (e) {
      if (/job\(s\) are running/.test(e.message)) setBusyJobs(e.message);
      else throw e;
    }
  };

  if (!net) return <p className="text-sm text-gray-500">Loading...</p>;
  return (
    <StepFrame
      {...nav}
      title="Who can open P3"
      intro="P3 runs on this Mac. You can keep it to this Mac, or let phones and computers on your home network open it too. Everyone still signs in with their own account."
      skipLabel="Keep it to this Mac"
      onSave={() => apply(false)}
    >
      <div className="space-y-2">
        <Choice name="lan" value="local" current={lan ? 'lan' : 'local'} onChange={() => setLan(false)} title="Only this Mac" />
        <Choice name="lan" value="lan" current={lan ? 'lan' : 'local'} onChange={() => setLan(true)} title="This Mac and other devices on my home network">
          Only turn this on at home or on a network you trust.
        </Choice>
      </div>
      {lan && (
        <div className="text-sm text-gray-700 space-y-1">
          <p>Other devices open P3 at:</p>
          <ul className="list-disc ml-5">
            {net.addresses.map((a) => <li key={a}><code>{a}</code></li>)}
          </ul>
          <p className="text-xs text-gray-500">
            If your Mac asks whether to allow incoming connections for Python, click Allow.
          </p>
        </div>
      )}
      {!net.managed && lan !== net.lan && (
        <p className="text-xs text-amber-700">Saved. Restart P3 for this to take effect.</p>
      )}
      {restarting && <p className="text-sm text-gray-600">Restarting P3... this takes a few seconds.</p>}
      {busyJobs && (
        <div className="text-sm text-amber-700 space-y-2">
          <p>{busyJobs}</p>
          <button type="button" onClick={() => apply(true)} className={secondaryButton}>Change it anyway</button>
        </div>
      )}
    </StepFrame>
  );
}

// ------------------------------------------------------------- podcasts

function PodcastsStep(nav) {
  return (
    <StepFrame
      {...nav}
      title="Add your podcasts"
      intro="Paste a podcast's link (Apple Podcasts, an RSS feed, or YouTube). P3 starts getting episodes right away; you can keep adding more here or later."
      skipLabel="I'll add podcasts later"
      saveLabel="Continue"
      onSave={async () => nav.onNext()}
    >
      <AddPodcast embedded />
    </StepFrame>
  );
}

// ----------------------------------------------------------------- done

function DoneStep({ onFinish, onBack }) {
  const [busy, setBusy] = useState(false);
  return (
    <div className="space-y-4">
      <h2 className="text-xl font-bold text-gray-900">You're all set</h2>
      <p className="text-sm text-gray-600">
        Everything you chose can be changed later in Settings, where you can also open this setup again.
        New episodes show up on the Dashboard as they're processed.
      </p>
      <div className="flex gap-3 pt-2 border-t">
        <button type="button" onClick={onBack} className="text-sm text-gray-500 hover:text-gray-800 mr-auto">← Back</button>
        <button type="button" disabled={busy} onClick={() => { setBusy(true); onFinish(); }} className={primaryButton}>
          Go to P3
        </button>
      </div>
    </div>
  );
}

// --------------------------------------------------------------- wizard

export default function Setup() {
  const { me, reload, authMode } = useMe();
  const navigate = useNavigate();
  const isAdmin = me.role === 'admin';

  const steps = [
    { key: 'language', label: 'Language', Component: LanguageStep },
    isAdmin && { key: 'summaries', label: 'Summaries', Component: SummariesStep },
    isAdmin && { key: 'transcription', label: 'Transcription', Component: TranscriptionStep },
    { key: 'daily', label: 'Daily digest', Component: DailyStep },
    isAdmin && authMode === 'local' && { key: 'network', label: 'Network', Component: NetworkStep },
    { key: 'podcasts', label: 'Podcasts', Component: PodcastsStep },
  ].filter(Boolean);

  const [index, setIndex] = useState(0);
  const finish = async () => {
    await finishSetup().catch(() => {});
    navigate('/');
  };

  const onNext = () => setIndex((i) => i + 1);
  const onBack = index > 0 ? () => setIndex((i) => i - 1) : undefined;
  const current = steps[index];

  return (
    <div className="min-h-screen bg-gray-100 py-6 px-4">
      <div className="max-w-2xl mx-auto">
        <div className="flex items-center justify-between mb-4">
          <div>
            <p className="text-xs font-semibold tracking-wide text-blue-600">P3 · Setup</p>
            <p className="text-sm text-gray-600">Hi {me.display_name || me.username}</p>
          </div>
          <button onClick={finish} className="text-sm text-gray-500 hover:text-gray-800 underline">
            Skip setup
          </button>
        </div>
        <ol className="flex flex-wrap gap-2 mb-4">
          {[...steps, { key: 'done', label: 'Done' }].map((s, i) => (
            <li key={s.key} className={`text-xs px-2 py-1 rounded-full ${
              i === index ? 'bg-blue-600 text-white' : i < index ? 'bg-blue-100 text-blue-800' : 'bg-white text-gray-500 border'
            }`}>
              {i + 1}. {s.label}
            </li>
          ))}
        </ol>
        <div className="bg-white rounded-xl shadow p-5 sm:p-6">
          {current ? (
            <current.Component key={current.key} me={me} reload={reload} onNext={onNext} onSkip={onNext} onBack={onBack} />
          ) : (
            <DoneStep onFinish={finish} onBack={onBack} />
          )}
        </div>
      </div>
    </div>
  );
}
