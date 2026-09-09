import { useEffect, useState } from 'react';
import { getSettings, updateSettings } from '../api/client';

export default function Settings() {
  const [settings, setSettings] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    getSettings()
      .then(setSettings)
      .catch((e) => setError(e.message));
  }, []);

  const handleChange = (key, value) => {
    setSettings((prev) => ({
      ...prev,
      settings: { ...prev.settings, [key]: value },
    }));
    setSaved(false);
  };

  const handleSave = async () => {
    setSaving(true);
    setSaved(false);
    try {
      const result = await updateSettings(settings);
      setSettings(result);
      setSaved(true);
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  };

  if (error) return <p className="text-red-600">Error: {error}</p>;
  if (!settings) return <p className="text-gray-500">Loading...</p>;

  const s = settings.settings || {};

  return (
    <div className="max-w-lg">
      <h1 className="text-2xl font-bold text-gray-900 mb-6">Settings</h1>

      <div className="space-y-4">
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">LLM Provider</label>
          <select
            value={s.llm_provider || 'ollama'}
            onChange={(e) => handleChange('llm_provider', e.target.value)}
            className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            <option value="ollama">Ollama (local)</option>
            <option value="openai">OpenAI</option>
          </select>
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">LLM Model</label>
          <input
            type="text"
            value={s.llm_model || ''}
            onChange={(e) => handleChange('llm_model', e.target.value)}
            placeholder="e.g. llama3.2:latest"
            className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Whisper Model</label>
          <select
            value={s.whisper_model || 'base'}
            onChange={(e) => handleChange('whisper_model', e.target.value)}
            className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            <option value="tiny">Tiny (fastest)</option>
            <option value="base">Base</option>
            <option value="small">Small</option>
            <option value="medium">Medium</option>
            <option value="large">Large (most accurate)</option>
          </select>
        </div>

        <div className="flex items-center gap-3">
          <input
            type="checkbox"
            id="parakeet"
            checked={s.parakeet_enabled || false}
            onChange={(e) => handleChange('parakeet_enabled', e.target.checked)}
            className="rounded"
          />
          <label htmlFor="parakeet" className="text-sm text-gray-700">
            Use Parakeet MLX for transcription (Apple Silicon)
          </label>
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            Max Episodes Per Feed
          </label>
          <input
            type="number"
            min="1"
            max="100"
            value={s.max_episodes_per_feed || 10}
            onChange={(e) => handleChange('max_episodes_per_feed', parseInt(e.target.value) || 10)}
            className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Audio Format</label>
          <select
            value={s.audio_format || 'wav'}
            onChange={(e) => handleChange('audio_format', e.target.value)}
            className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            <option value="wav">WAV</option>
            <option value="mp3">MP3</option>
          </select>
        </div>

        <button
          onClick={handleSave}
          disabled={saving}
          className="w-full px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 disabled:opacity-50 font-medium"
        >
          {saving ? 'Saving...' : 'Save Settings'}
        </button>

        {saved && (
          <p className="text-sm text-green-600 text-center">Settings saved.</p>
        )}
      </div>
    </div>
  );
}
