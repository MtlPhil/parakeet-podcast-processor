import { useCallback, useEffect, useState } from 'react';
import { Routes, Route } from 'react-router-dom';
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
import { getMe } from './api/client';
import { MeContext } from './hooks/useMe';

export default function App() {
  const [me, setMe] = useState(null);
  const [error, setError] = useState(null);

  const reload = useCallback(
    () => getMe().then(setMe).catch((e) => setError(e.message)),
    []
  );
  useEffect(() => { reload(); }, [reload]);

  if (error && !me) {
    return (
      <p className="p-6 text-red-600">
        {error === 'Not signed in'
          ? 'You are not signed in. Open P3 through its sign-in page.'
          : `Error: ${error}`}
      </p>
    );
  }
  if (!me) return <p className="p-6 text-gray-500">Loading...</p>;

  return (
    <MeContext.Provider value={{ me, reload }}>
      <Routes>
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
