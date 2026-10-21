import { useCallback, useEffect, useState } from 'react';
import { Routes, Route, useNavigate } from 'react-router-dom';
import Layout from './components/Layout';
import Dashboard from './pages/Dashboard';
import AddPodcast from './pages/AddPodcast';
import PodcastList from './pages/PodcastList';
import PodcastDetail from './pages/PodcastDetail';
import EpisodeDetail from './pages/EpisodeDetail';
import BlogPosts from './pages/BlogPosts';
import Settings from './pages/Settings';
import Search from './pages/Search';
import Admin from './pages/Admin';
import Setup from './pages/Setup';
import { CreateAdmin, Login } from './pages/SignIn';
import { getAuthStatus, getMe, getSetupProgress } from './api/client';
import { MeContext } from './hooks/useMe';

export default function App() {
  const [status, setStatus] = useState(null);
  const [me, setMe] = useState(null);
  const [error, setError] = useState(null);
  const [attempt, setAttempt] = useState(0);
  const navigate = useNavigate();

  const reload = useCallback(
    () => getMe().then(setMe).catch((e) => setError(e.message)),
    []
  );

  // Look again (after creating the first account or signing in).
  const start = () => {
    setError(null);
    setAttempt((a) => a + 1);
  };

  // Who is here? Installs with their own accounts may need their first
  // account or a sign-in before anything else.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const s = await getAuthStatus();
        if (cancelled) return;
        setStatus(s);
        if (s.mode === 'local' && (s.setup_needed || !s.signed_in)) return;
        const user = await getMe();
        if (cancelled) return;
        setMe(user);
        // First visit to an install with its own accounts: the setup wizard.
        if (s.mode === 'local') {
          const progress = await getSetupProgress();
          if (!cancelled && !progress.done && window.location.pathname !== '/setup') {
            navigate('/setup');
          }
        }
      } catch (e) {
        if (!cancelled) setError(e.message);
      }
    })();
    return () => { cancelled = true; };
  }, [attempt, navigate]);

  if (error && !me) {
    return (
      <p className="p-6 text-red-600">
        {error === 'Not signed in'
          ? 'You are not signed in. Open P3 through its sign-in page.'
          : `Error: ${error}`}
      </p>
    );
  }
  if (!status) return <p className="p-6 text-gray-500">Loading...</p>;
  if (status.mode === 'local' && status.setup_needed) return <CreateAdmin onCreated={start} />;
  if (status.mode === 'local' && !status.signed_in) return <Login onSignedIn={start} />;
  if (!me) return <p className="p-6 text-gray-500">Loading...</p>;

  return (
    <MeContext.Provider value={{ me, reload, authMode: status.mode }}>
      <Routes>
        <Route path="/setup" element={<Setup />} />
        <Route element={<Layout />}>
          <Route path="/" element={<Dashboard />} />
          <Route path="/add" element={<AddPodcast />} />
          <Route path="/podcasts" element={<PodcastList />} />
          <Route path="/podcasts/:id" element={<PodcastDetail />} />
          <Route path="/episodes/:id" element={<EpisodeDetail />} />
          <Route path="/blogs" element={<BlogPosts />} />
          <Route path="/search" element={<Search />} />
          <Route path="/settings" element={<Settings />} />
          {me.role === 'admin' && <Route path="/admin" element={<Admin />} />}
        </Route>
      </Routes>
    </MeContext.Provider>
  );
}
