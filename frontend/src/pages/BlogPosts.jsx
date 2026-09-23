import { useEffect, useState } from 'react';
import {
  getBlogs, getBlog, createBlog,
  getLinkedInPosts, getLinkedInPost, createLinkedInPost,
  getEpisodes,
} from '../api/client';
import { useJobPoller } from '../hooks/useJobPoller';
import JobProgress from '../components/JobProgress';
import ProviderSelect from '../components/ProviderSelect';

export default function BlogPosts() {
  const [mode, setMode] = useState('linkedin'); // 'linkedin' (default) or 'blog'

  // LinkedIn state
  const [linkedinPosts, setLinkedinPosts] = useState([]);
  const [selectedLinkedin, setSelectedLinkedin] = useState(null);
  const [episodes, setEpisodes] = useState([]);
  const [podcastId, setPodcastId] = useState('');
  const [episodeId, setEpisodeId] = useState('');
  const [linkedinProvider, setLinkedinProvider] = useState('');

  // Blog (theme-based) state
  const [blogs, setBlogs] = useState([]);
  const [selectedBlog, setSelectedBlog] = useState(null);
  const [topic, setTopic] = useState('');
  const [date, setDate] = useState('');
  const [blogProvider, setBlogProvider] = useState('');

  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState(null);
  const { job, isPolling, startPolling } = useJobPoller();

  const loadLinkedin = async () => {
    try {
      setLinkedinPosts(await getLinkedInPosts());
    } catch (e) {
      setError(e.message);
    }
  };

  const loadBlogs = async () => {
    try {
      setBlogs(await getBlogs());
    } catch (e) {
      setError(e.message);
    }
  };

  useEffect(() => {
    loadLinkedin();
    loadBlogs();
    getEpisodes({ status: 'processed' }).then(setEpisodes).catch((e) => setError(e.message));
  }, []);

  useEffect(() => {
    if (job?.status === 'completed') {
      loadLinkedin();
      loadBlogs();
    }
  }, [job?.status]);

  // Distinct podcasts derived from the processed-episode list, for the
  // first-level dropdown.
  const podcasts = Object.values(
    episodes.reduce((acc, ep) => {
      acc[ep.podcast_id] = { id: ep.podcast_id, title: ep.podcast_title };
      return acc;
    }, {})
  ).sort((a, b) => (a.title || '').localeCompare(b.title || ''));

  const episodesForPodcast = episodes
    .filter((ep) => String(ep.podcast_id) === String(podcastId))
    .sort((a, b) => (b.date || '').localeCompare(a.date || ''));

  const handlePodcastChange = (id) => {
    setPodcastId(id);
    setEpisodeId('');
  };

  const handleViewLinkedin = async (slug) => {
    try {
      setSelectedLinkedin(await getLinkedInPost(slug));
    } catch (e) {
      setError(e.message);
    }
  };

  const handleViewBlog = async (slug) => {
    try {
      setSelectedBlog(await getBlog(slug));
    } catch (e) {
      setError(e.message);
    }
  };

  const handleCreateLinkedin = async (e) => {
    e.preventDefault();
    try {
      const result = await createLinkedInPost({
        episode_id: Number(episodeId),
        provider: linkedinProvider || undefined,
      });
      startPolling(result.job_id);
      setShowForm(false);
    } catch (e) {
      setError(e.message);
    }
  };

  const handleCreateBlog = async (e) => {
    e.preventDefault();
    try {
      const result = await createBlog({
        topic,
        date: date || undefined,
        target_grade: 91.0,
        provider: blogProvider || undefined,
      });
      startPolling(result.job_id);
      setShowForm(false);
    } catch (e) {
      setError(e.message);
    }
  };

  if (selectedLinkedin) {
    return (
      <div>
        <button
          onClick={() => setSelectedLinkedin(null)}
          className="text-sm text-blue-600 hover:underline mb-4"
        >
          &larr; Back to list
        </button>
        <h1 className="text-2xl font-bold text-gray-900 mb-1">{selectedLinkedin.title}</h1>
        <p className="text-sm text-gray-500 mb-4">
          {selectedLinkedin.date}
          {selectedLinkedin.podcast_title && ` · ${selectedLinkedin.podcast_title}`}
        </p>
        <div className="prose prose-sm max-w-none text-gray-700 whitespace-pre-wrap">
          {selectedLinkedin.content}
        </div>
      </div>
    );
  }

  if (selectedBlog) {
    return (
      <div>
        <button
          onClick={() => setSelectedBlog(null)}
          className="text-sm text-blue-600 hover:underline mb-4"
        >
          &larr; Back to list
        </button>
        <h1 className="text-2xl font-bold text-gray-900 mb-1">{selectedBlog.title}</h1>
        <p className="text-sm text-gray-500 mb-4">
          {selectedBlog.date}
          {selectedBlog.final_grade && ` · Grade: ${selectedBlog.final_grade}`}
          {selectedBlog.final_score && ` (${selectedBlog.final_score}/100)`}
        </p>
        <div className="prose prose-sm max-w-none text-gray-700 whitespace-pre-wrap">
          {selectedBlog.content}
        </div>
      </div>
    );
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-gray-900">Content</h1>
        <button
          onClick={() => setShowForm(!showForm)}
          className="px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700"
        >
          + Generate New
        </button>
      </div>

      <div className="flex gap-1 mb-6 border-b">
        <button
          onClick={() => setMode('linkedin')}
          className={`px-4 py-2 text-sm font-medium border-b-2 ${
            mode === 'linkedin'
              ? 'border-blue-600 text-blue-600'
              : 'border-transparent text-gray-500 hover:text-gray-700'
          }`}
        >
          LinkedIn Posts
        </button>
        <button
          onClick={() => setMode('blog')}
          className={`px-4 py-2 text-sm font-medium border-b-2 ${
            mode === 'blog'
              ? 'border-blue-600 text-blue-600'
              : 'border-transparent text-gray-500 hover:text-gray-700'
          }`}
        >
          Blog Posts (theme-based)
        </button>
      </div>

      {error && <p className="text-red-600 text-sm mb-4">{error}</p>}

      {showForm && mode === 'linkedin' && (
        <form onSubmit={handleCreateLinkedin} className="bg-white border rounded-lg p-4 mb-6 space-y-3">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Podcast *</label>
            <select
              value={podcastId}
              onChange={(e) => handlePodcastChange(e.target.value)}
              required
              className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="" disabled>Select a podcast…</option>
              {podcasts.map((p) => (
                <option key={p.id} value={p.id}>{p.title}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Episode *</label>
            <select
              value={episodeId}
              onChange={(e) => setEpisodeId(e.target.value)}
              required
              disabled={!podcastId}
              className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-100"
            >
              <option value="" disabled>
                {podcastId ? 'Select an episode…' : 'Select a podcast first'}
              </option>
              {episodesForPodcast.map((ep) => (
                <option key={ep.id} value={ep.id}>
                  {(ep.date || '').slice(0, 10)} - {ep.title}
                </option>
              ))}
            </select>
            <p className="text-xs text-gray-500 mt-1">
              Generates one post in English and one in Quebec French, based on this episode's summary.
            </p>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Model</label>
            <ProviderSelect value={linkedinProvider} onChange={setLinkedinProvider} className="w-full" />
          </div>
          <button
            type="submit"
            disabled={isPolling || !episodeId}
            className="px-4 py-2 bg-purple-600 text-white text-sm rounded-lg hover:bg-purple-700 disabled:opacity-50"
          >
            {isPolling ? 'Generating...' : 'Generate LinkedIn Post'}
          </button>
        </form>
      )}

      {showForm && mode === 'blog' && (
        <form onSubmit={handleCreateBlog} className="bg-white border rounded-lg p-4 mb-6 space-y-3">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Topic *</label>
            <input
              type="text"
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              required
              placeholder="e.g. AI's Impact on Software Development"
              className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Source Date (YYYY-MM-DD, defaults to today)
            </label>
            <input
              type="date"
              value={date}
              onChange={(e) => setDate(e.target.value)}
              className="w-full max-w-[200px] border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 box-border"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Model</label>
            <ProviderSelect value={blogProvider} onChange={setBlogProvider} className="w-full" />
          </div>
          <button
            type="submit"
            disabled={isPolling}
            className="px-4 py-2 bg-purple-600 text-white text-sm rounded-lg hover:bg-purple-700 disabled:opacity-50"
          >
            {isPolling ? 'Generating...' : 'Generate Blog Post'}
          </button>
        </form>
      )}

      {job && <div className="mb-6"><JobProgress job={job} /></div>}

      {mode === 'linkedin' && (
        linkedinPosts.length === 0 ? (
          <p className="text-gray-500 text-sm">No LinkedIn posts yet.</p>
        ) : (
          <div className="space-y-3">
            {linkedinPosts.map((p) => (
              <div
                key={p.slug}
                className="bg-white border rounded-lg p-4 hover:bg-gray-50 cursor-pointer"
                onClick={() => handleViewLinkedin(p.slug)}
              >
                <h3 className="font-medium text-gray-900">{p.title}</h3>
                <p className="text-sm text-gray-500 mt-1">
                  {p.date}
                  {p.podcast_title && ` · ${p.podcast_title}`}
                </p>
              </div>
            ))}
          </div>
        )
      )}

      {mode === 'blog' && (
        blogs.length === 0 ? (
          <p className="text-gray-500 text-sm">No blog posts yet.</p>
        ) : (
          <div className="space-y-3">
            {blogs.map((b) => (
              <div
                key={b.slug}
                className="bg-white border rounded-lg p-4 hover:bg-gray-50 cursor-pointer"
                onClick={() => handleViewBlog(b.slug)}
              >
                <h3 className="font-medium text-gray-900">{b.title}</h3>
                <p className="text-sm text-gray-500 mt-1">
                  {b.date}
                  {b.final_grade && ` · Grade: ${b.final_grade}`}
                  {b.final_score && ` (${b.final_score}/100)`}
                </p>
              </div>
            ))}
          </div>
        )
      )}
    </div>
  );
}
