import { useEffect, useState } from 'react';
import { discoverPodcasts, getDirectoryCategories } from '../api/client';

const COUNTRIES = [
  ['us', 'United States'],
  ['ca', 'Canada'],
  ['fr', 'France'],
  ['gb', 'United Kingdom'],
  ['au', 'Australia'],
];
const COUNTRY_KEY = 'p3.discover.country';

const savedCountry = () => {
  try {
    return localStorage.getItem(COUNTRY_KEY) || 'us';
  } catch {
    return 'us';
  }
};

// Search Apple's podcast directory, or browse a category's top chart; onPick
// gets the chosen show's feed URL and name (the episode preview follows).
export default function DiscoverPodcasts({ onPick }) {
  const [categories, setCategories] = useState([]);
  const [term, setTerm] = useState('');
  const [genre, setGenre] = useState('');
  const [country, setCountry] = useState(savedCountry);
  const [shows, setShows] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    getDirectoryCategories().then(setCategories).catch(() => {});
  }, []);

  const find = async (e) => {
    e?.preventDefault();
    if (!term.trim() && !genre) return;
    setLoading(true);
    setError(null);
    try {
      setShows(await discoverPodcasts({ q: term.trim(), genre, country }));
    } catch (err) {
      setError(err.message);
      setShows(null);
    } finally {
      setLoading(false);
    }
  };

  const pickCountry = (value) => {
    setCountry(value);
    try {
      localStorage.setItem(COUNTRY_KEY, value);
    } catch {
      // Private window: the choice lasts for this page only.
    }
  };

  const inputClass = 'w-full border border-gray-300 rounded-lg px-3 py-2 text-sm';
  return (
    <div className="space-y-4">
      <form onSubmit={find} className="space-y-3">
        <input
          type="search"
          value={term}
          onChange={(e) => setTerm(e.target.value)}
          placeholder="Podcast name, host or subject (empty: the category's top shows)"
          aria-label="Search the podcast directory"
          className={inputClass}
        />
        <div className="flex flex-wrap gap-2">
          <select value={genre} onChange={(e) => setGenre(e.target.value)} aria-label="Category" className="border border-gray-300 rounded-lg px-2 py-2 text-sm flex-1 min-w-[10rem]">
            <option value="">Any category</option>
            {categories.map((c) => (
              <option key={c.id} value={c.id}>{c.name}</option>
            ))}
          </select>
          <select value={country} onChange={(e) => pickCountry(e.target.value)} aria-label="Directory country" className="border border-gray-300 rounded-lg px-2 py-2 text-sm">
            {COUNTRIES.map(([code, label]) => (
              <option key={code} value={code}>{label}</option>
            ))}
          </select>
          <button
            type="submit"
            disabled={loading || (!term.trim() && !genre)}
            className="px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 disabled:opacity-50"
          >
            {loading ? 'Searching...' : term.trim() ? 'Search' : 'Top shows'}
          </button>
        </div>
        <p className="text-xs text-gray-500">From Apple&apos;s podcast directory: it gets your search words, category and country, and your browser loads the artwork from Apple.</p>
      </form>

      {error && <p className="text-sm text-red-600">{error}</p>}
      {shows && !shows.length && <p className="text-sm text-gray-500">Nothing found.</p>}
      {shows?.length > 0 && (
        <ul className="divide-y border border-gray-200 rounded-lg">
          {shows.map((s) => (
            <li key={s.feed_url} className="flex items-center gap-3 p-2">
              {s.artwork ? (
                <img src={s.artwork} alt="" loading="lazy" className="w-12 h-12 rounded object-cover flex-none" />
              ) : (
                <div className="w-12 h-12 rounded bg-gray-100 flex-none" />
              )}
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-gray-800 truncate">{s.name}</p>
                <p className="text-xs text-gray-500 truncate">
                  {[s.author, s.genre, s.episode_count && `${s.episode_count} episodes`].filter(Boolean).join(' · ')}
                </p>
              </div>
              {s.in_library ? (
                <span className="text-xs text-gray-500">Following</span>
              ) : (
                <button
                  type="button"
                  onClick={() => onPick(s.feed_url, s.name)}
                  className="px-3 py-1 text-xs bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200"
                >
                  Preview
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
