const BASE = '/api';

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `API error ${res.status}`);
  }
  // Handle 204 No Content
  if (res.status === 204) return null;
  return res.json();
}

// Stats
export const getStats = () => request('/stats');

// Podcasts
export const getPodcasts = () => request('/podcasts');
export const getPodcast = (id) => request(`/podcasts/${id}`);
export const addPodcast = (data) =>
  request('/podcasts', { method: 'POST', body: JSON.stringify(data) });
export const updatePodcast = (id, data) =>
  request(`/podcasts/${id}`, { method: 'PATCH', body: JSON.stringify(data) });
export const deletePodcast = (id) =>
  request(`/podcasts/${id}`, { method: 'DELETE' });
export const fetchPodcast = (id, data = {}) =>
  request(`/podcasts/${id}/fetch`, { method: 'POST', body: JSON.stringify(data) });

// Episodes
export const getEpisodes = (params = {}) => {
  const qs = new URLSearchParams(params).toString();
  return request(`/episodes${qs ? '?' + qs : ''}`);
};
export const getEpisode = (id) => request(`/episodes/${id}`);
export const transcribeEpisode = (id) =>
  request(`/episodes/${id}/transcribe`, { method: 'POST' });
export const digestEpisode = (id) =>
  request(`/episodes/${id}/digest`, { method: 'POST' });
export const runPipeline = (id) =>
  request(`/episodes/${id}/pipeline`, { method: 'POST' });

// Batch pipeline triggers — one job is queued per eligible episode; the
// backend job runner executes them one at a time.
export const processPodcast = (id, step) =>
  request(`/podcasts/${id}/process/${step}`, { method: 'POST' });
export const processLibrary = (step) =>
  request(`/episodes/process/${step}`, { method: 'POST' });

// Transcripts
export const getTranscript = (episodeId) =>
  request(`/episodes/${episodeId}/transcript`);

// Summaries
export const getSummary = (episodeId) =>
  request(`/episodes/${episodeId}/summary`);
export const getSummaries = () => request('/summaries');

// Jobs
export const getJobs = (activeOnly = false) =>
  request(`/jobs${activeOnly ? '?active_only=true' : ''}`);
export const getJob = (id) => request(`/jobs/${id}`);
export const retryJob = (id) =>
  request(`/jobs/${id}/retry`, { method: 'POST' });
export const retryFailedJobs = () =>
  request('/jobs/retry-failed', { method: 'POST' });
export const clearJobs = () => request('/jobs', { method: 'DELETE' });
export const clearFailedJobs = () => request('/jobs/failed', { method: 'DELETE' });

// Exports
export const generateExport = (date, formats = 'markdown,json') =>
  request(`/exports?date=${date}&formats=${formats}`, { method: 'POST' });

// Blogs
export const getBlogs = () => request('/blogs');
export const getBlog = (slug) => request(`/blogs/${slug}`);
export const createBlog = (data) =>
  request('/blogs', { method: 'POST', body: JSON.stringify(data) });

// LinkedIn posts
export const getLinkedInPosts = () => request('/linkedin');
export const getLinkedInPost = (slug) => request(`/linkedin/${slug}`);
export const createLinkedInPost = (data) =>
  request('/linkedin', { method: 'POST', body: JSON.stringify(data) });

// Settings
export const getSettings = () => request('/settings');
export const updateSettings = (data) =>
  request('/settings', { method: 'PUT', body: JSON.stringify(data) });
