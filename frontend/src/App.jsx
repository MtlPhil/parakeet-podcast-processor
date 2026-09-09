import { Routes, Route } from 'react-router-dom';
import Layout from './components/Layout';
import Dashboard from './pages/Dashboard';
import AddPodcast from './pages/AddPodcast';
import PodcastList from './pages/PodcastList';
import PodcastDetail from './pages/PodcastDetail';
import EpisodeDetail from './pages/EpisodeDetail';
import BlogPosts from './pages/BlogPosts';
import Settings from './pages/Settings';

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/add" element={<AddPodcast />} />
        <Route path="/podcasts" element={<PodcastList />} />
        <Route path="/podcasts/:id" element={<PodcastDetail />} />
        <Route path="/episodes/:id" element={<EpisodeDetail />} />
        <Route path="/blogs" element={<BlogPosts />} />
        <Route path="/settings" element={<Settings />} />
      </Route>
    </Routes>
  );
}
