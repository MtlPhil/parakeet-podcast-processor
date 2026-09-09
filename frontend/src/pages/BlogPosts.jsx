import { useEffect, useState } from 'react';
import { getBlogs, getBlog, createBlog } from '../api/client';
import { useJobPoller } from '../hooks/useJobPoller';
import JobProgress from '../components/JobProgress';

export default function BlogPosts() {
  const [blogs, setBlogs] = useState([]);
  const [selected, setSelected] = useState(null);
  const [showForm, setShowForm] = useState(false);
  const [topic, setTopic] = useState('');
  const [date, setDate] = useState('');
  const [error, setError] = useState(null);
  const { job, isPolling, startPolling } = useJobPoller();

  const load = async () => {
    try {
      setBlogs(await getBlogs());
    } catch (e) {
      setError(e.message);
    }
  };

  useEffect(() => { load(); }, []);

  useEffect(() => {
    if (job?.status === 'completed') load();
  }, [job?.status]);

  const handleView = async (slug) => {
    try {
      const blog = await getBlog(slug);
      setSelected(blog);
    } catch (e) {
      setError(e.message);
    }
  };

  const handleCreate = async (e) => {
    e.preventDefault();
    try {
      const result = await createBlog({
        topic,
        date: date || undefined,
        target_grade: 91.0,
      });
      startPolling(result.job_id);
      setShowForm(false);
    } catch (e) {
      setError(e.message);
    }
  };

  if (selected) {
    return (
      <div>
        <button
          onClick={() => setSelected(null)}
          className="text-sm text-blue-600 hover:underline mb-4"
        >
          &larr; Back to list
        </button>
        <h1 className="text-2xl font-bold text-gray-900 mb-1">{selected.title}</h1>
        <p className="text-sm text-gray-500 mb-4">
          {selected.date}
          {selected.final_grade && ` · Grade: ${selected.final_grade}`}
          {selected.final_score && ` (${selected.final_score}/100)`}
        </p>
        <div className="prose prose-sm max-w-none text-gray-700 whitespace-pre-wrap">
          {selected.content}
        </div>
      </div>
    );
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-gray-900">Blog Posts</h1>
        <button
          onClick={() => setShowForm(!showForm)}
          className="px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700"
        >
          + Generate New
        </button>
      </div>

      {error && <p className="text-red-600 text-sm mb-4">{error}</p>}

      {showForm && (
        <form onSubmit={handleCreate} className="bg-white border rounded-lg p-4 mb-6 space-y-3">
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
              className="w-full border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
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

      {blogs.length === 0 ? (
        <p className="text-gray-500 text-sm">No blog posts yet.</p>
      ) : (
        <div className="space-y-3">
          {blogs.map((b) => (
            <div
              key={b.slug}
              className="bg-white border rounded-lg p-4 hover:bg-gray-50 cursor-pointer"
              onClick={() => handleView(b.slug)}
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
      )}
    </div>
  );
}
