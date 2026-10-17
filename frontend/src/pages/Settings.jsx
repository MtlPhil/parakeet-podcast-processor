import { useEffect, useState } from 'react';
import { getSettings, updateMySettings, updateSettings } from '../api/client';
import { useMe } from '../hooks/useMe';

const inputClass =
  'w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500';

function Field({ label, htmlFor, help, children }) {
  return (
    <div>
      <label htmlFor={htmlFor} className="block text-sm font-medium text-gray-700 mb-1">{label}</label>
      {children}
      {help && <p className="text-xs text-gray-500 mt-1">{help}</p>}
    </div>
  );
}

function Checkbox({ id, checked, onChange, children }) {
  return (
    <div className="flex items-center gap-3">
      <input type="checkbox" id={id} checked={checked} onChange={(e) => onChange(e.target.checked)} className="rounded" />
      <label htmlFor={id} className="text-sm text-gray-700">{children}</label>
    </div>
  );
}

function SaveButton({ saving, saved, onClick, label }) {
  return (
    <>
      <button
        onClick={onClick}
        disabled={saving}
        className="w-full px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 disabled:opacity-50 font-medium"
      >
        {saving ? 'Saving...' : label}
      </button>
      {saved && <p className="text-sm text-green-600 text-center">Saved.</p>}
    </>
  );
}

// The user's own settings. Empty fields fall back to the shared settings,
// shown as placeholders.
function MySettings() {
  const { me, reload } = useMe();
  const [own, setOwn] = useState(me.own_settings);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState(null);
  const effective = me.settings;

  const set = (key, value) => {
    setOwn((prev) => ({ ...prev, [key]: value }));
    setSaved(false);
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await updateMySettings(own);
      await reload();
      setSaved(true);
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="space-y-4">
      <h2 className="text-lg font-semibold text-gray-800">Your settings</h2>

      <Field label="Language" htmlFor="language" help="Summaries and posts show in this language first; every page can switch to the other.">
        <select id="language" value={own.language || effective.language} onChange={(e) => set('language', e.target.value)} className={inputClass}>
          <option value="en">English</option>
          <option value="fr">Français (Québec)</option>
        </select>
      </Field>

      <Field label="Writing tone" htmlFor="tone" help="Describe your voice. It is sent to the writer with every LinkedIn and blog post you generate.">
        <textarea
          id="tone"
          rows={4}
          value={own.tone || ''}
          onChange={(e) => set('tone', e.target.value)}
          placeholder="e.g. Plain-spoken and practical. Short sentences, no buzzwords, one concrete example per post."
          className={inputClass}
        />
      </Field>

      <Field label="Writing LLM provider" htmlFor="llm-provider" help="Used for your posts and synopses.">
        <select id="llm-provider" value={own.llm_provider || ''} onChange={(e) => set('llm_provider', e.target.value || null)} className={inputClass}>
          <option value="">Default ({effective.llm_provider})</option>
          <option value="ollama">Ollama (local)</option>
          <option value="openai">OpenAI</option>
          <option value="gemini">Gemini</option>
        </select>
      </Field>

      <Field label="Writing LLM model" htmlFor="llm-model">
        <input id="llm-model" type="text" value={own.llm_model || ''} onChange={(e) => set('llm_model', e.target.value)} placeholder={effective.llm_model || 'Default'} className={inputClass} />
      </Field>

      <Field label="Gemini model" htmlFor="gemini-model">
        <input id="gemini-model" type="text" value={own.gemini_model || ''} onChange={(e) => set('gemini_model', e.target.value)} placeholder={effective.gemini_model || 'Used when the provider is Gemini'} className={inputClass} />
      </Field>

      <Field label="Max episodes per feed" htmlFor="max-episodes" help="How many of the newest episodes each fetch of your podcasts takes.">
        <input
          id="max-episodes"
          type="number"
          min="1"
          max="100"
          value={own.max_episodes_per_feed || ''}
          onChange={(e) => set('max_episodes_per_feed', parseInt(e.target.value) || null)}
          placeholder={String(effective.max_episodes_per_feed)}
          className={inputClass}
        />
      </Field>

      <Field label="Daily run time" htmlFor="daily-run-time" help="Each day at this time, fetch your podcasts, then transcribe and summarize new episodes. Runs on wake if the Mac was asleep. Clear to turn it off.">
        <input id="daily-run-time" type="time" value={own.daily_run_time || ''} onChange={(e) => set('daily_run_time', e.target.value)} className={inputClass} />
      </Field>

      <Checkbox id="daily-run-linkedin" checked={own.daily_run_linkedin || false} onChange={(v) => set('daily_run_linkedin', v)}>
        Daily run: draft a LinkedIn post for each new episode
      </Checkbox>
      <Checkbox id="daily-run-export" checked={own.daily_run_export || false} onChange={(v) => set('daily_run_export', v)}>
        Daily run: export the day&apos;s digest (Markdown and JSON)
      </Checkbox>

      {error && <p className="text-sm text-red-600">{error}</p>}
      <SaveButton saving={saving} saved={saved} onClick={save} label="Save your settings" />
    </section>
  );
}

// Settings for work done once per episode for everyone. Admins only.
function SharedSettings() {
  const [config, setConfig] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    getSettings().then(setConfig).catch((e) => setError(e.message));
  }, []);

  if (error && !config) return <p className="text-red-600 text-sm">Error: {error}</p>;
  if (!config) return <p className="text-gray-500 text-sm">Loading...</p>;
  const s = config.settings || {};

  const set = (key, value) => {
    setConfig((prev) => ({ ...prev, settings: { ...prev.settings, [key]: value } }));
    setSaved(false);
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      setConfig(await updateSettings(config));
      setSaved(true);
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold text-gray-800">Shared settings</h2>
        <p className="text-xs text-gray-500">
          Admins only. These apply to everyone: each episode is transcribed and summarized
          once, whoever follows it. LLM and episode limits here are also the defaults for
          users who haven&apos;t set their own.
        </p>
      </div>

      <Field label="Summarizing LLM provider" htmlFor="shared-provider">
        <select id="shared-provider" value={s.llm_provider || 'ollama'} onChange={(e) => set('llm_provider', e.target.value)} className={inputClass}>
          <option value="ollama">Ollama (local)</option>
          <option value="openai">OpenAI</option>
          <option value="gemini">Gemini</option>
        </select>
      </Field>
      <Field label="Summarizing LLM model" htmlFor="shared-model">
        <input id="shared-model" type="text" value={s.llm_model || ''} onChange={(e) => set('llm_model', e.target.value)} placeholder="e.g. llama3.2:latest" className={inputClass} />
      </Field>
      <Field label="Gemini model" htmlFor="shared-gemini">
        <input id="shared-gemini" type="text" value={s.gemini_model || ''} onChange={(e) => set('gemini_model', e.target.value)} placeholder="Used when the provider is Gemini" className={inputClass} />
      </Field>
      <Field label="Ollama URL" htmlFor="ollama-url">
        <input id="ollama-url" type="text" value={s.ollama_base_url || ''} onChange={(e) => set('ollama_base_url', e.target.value)} placeholder="http://localhost:11434" className={inputClass} />
      </Field>
      <Field label="Whisper model" htmlFor="whisper">
        <select id="whisper" value={s.whisper_model || 'base'} onChange={(e) => set('whisper_model', e.target.value)} className={inputClass}>
          <option value="tiny">Tiny (fastest)</option>
          <option value="base">Base</option>
          <option value="small">Small</option>
          <option value="medium">Medium</option>
          <option value="large">Large (most accurate)</option>
        </select>
      </Field>
      <Checkbox id="parakeet" checked={s.parakeet_enabled || false} onChange={(v) => set('parakeet_enabled', v)}>
        Use Parakeet MLX for transcription (Apple Silicon)
      </Checkbox>
      <Field label="Default max episodes per feed" htmlFor="shared-max">
        <input
          id="shared-max"
          type="number"
          min="1"
          max="100"
          value={s.max_episodes_per_feed || 10}
          onChange={(e) => set('max_episodes_per_feed', parseInt(e.target.value) || 10)}
          className={inputClass}
        />
      </Field>
      <Field label="Audio format" htmlFor="audio-format">
        <select id="audio-format" value={s.audio_format || 'wav'} onChange={(e) => set('audio_format', e.target.value)} className={inputClass}>
          <option value="wav">WAV</option>
          <option value="mp3">MP3</option>
        </select>
      </Field>

      {error && <p className="text-sm text-red-600">{error}</p>}
      <SaveButton saving={saving} saved={saved} onClick={save} label="Save shared settings" />
    </section>
  );
}

export default function Settings() {
  const { me } = useMe();
  return (
    <div className="max-w-lg space-y-10">
      <h1 className="text-2xl font-bold text-gray-900">Settings</h1>
      <MySettings />
      {me.role === 'admin' && <SharedSettings />}
    </div>
  );
}
