const BASE = '/api';

// Reads the server-chosen filename from a Content-Disposition header. Blob
// URLs ignore that header, so it is applied via the <a download> attribute.
// The real name comes percent-encoded in filename*; filename is its ASCII
// approximation.
function filenameFromResponse(res, fallback) {
  const header = res.headers.get('Content-Disposition') || '';
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (encoded) {
    try {
      return decodeURIComponent(encoded[1]);
    } catch {
      // Malformed: fall through to the plain name.
    }
  }
  const plain = /filename="?([^";]+)"?/.exec(header);
  return plain ? plain[1] : fallback;
}

// The error for a failed response, carrying its HTTP status. FastAPI sends
// `detail` as a string, or for a rejected field as a list of problems.
async function errorFrom(res) {
  const { detail } = await res.json().catch(() => ({}));
  const message = Array.isArray(detail)
    ? detail.map((d) => [d.loc?.at(-1), d.msg].filter(Boolean).join(': ')).join('; ')
    : detail;
  const error = new Error(message || `API error ${res.status}`);
  error.status = res.status;
  return error;
}

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options.headers },
  });
  if (!res.ok) throw await errorFrom(res);
  // Handle 204 No Content
  if (res.status === 204) return null;
  return res.json();
}

// Saves a file the API returns, under the name the server gives it.
async function download(path, options, fallback) {
  const res = await fetch(`${BASE}${path}`, options);
  if (!res.ok) throw await errorFrom(res);
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement('a');
  a.href = url;
  a.download = filenameFromResponse(res, fallback);
  a.click();
  URL.revokeObjectURL(url);
}

// The signed-in user (a local account, or the reverse proxy's Remote-User)
export const getMe = () => request('/me');
export const deleteMyAccount = () => request('/me', { method: 'DELETE' });
export const updateMySettings = (settings) =>
  request('/me/settings', { method: 'PUT', body: JSON.stringify(settings) });

export const getDeletedEpisodes = () => request('/me/deleted-episodes');
export const allowDeletedEpisode = (url) =>
  request('/me/deleted-episodes/allow', { method: 'POST', body: JSON.stringify({ url }) });

// Admin
export const getUsers = () => request('/admin/users');
export const setUserRole = (username, role) =>
  request(`/admin/users/${encodeURIComponent(username)}`, {
    method: 'PATCH',
    body: JSON.stringify({ role }),
  });
export const deleteUser = (username) =>
  request(`/admin/users/${encodeURIComponent(username)}`, { method: 'DELETE' });
export const translateAll = () => request('/episodes/translate-all', { method: 'POST' });

// Stats
export const getStats = () => request('/stats');

// Podcasts
export const getPodcasts = () => request('/podcasts');
export const getPodcast = (id) => request(`/podcasts/${id}`);
export const addPodcast = (data) =>
  request('/podcasts', { method: 'POST', body: JSON.stringify(data) });
export const previewSource = (url) =>
  request(`/podcasts/preview?${new URLSearchParams({ url })}`);
export const updatePodcast = (id, data) =>
  request(`/podcasts/${id}`, { method: 'PATCH', body: JSON.stringify(data) });
export const deletePodcast = (id) =>
  request(`/podcasts/${id}`, { method: 'DELETE' });
export const bulkAddPodcasts = (urls, category) =>
  request('/podcasts/bulk', { method: 'POST', body: JSON.stringify({ urls, category }) });
export const getAvailableEpisodes = (id) => request(`/podcasts/${id}/available`);
export const fetchPodcast = (id, data = {}) =>
  request(`/podcasts/${id}/fetch`, { method: 'POST', body: JSON.stringify(data) });

// Episodes
export const getEpisodes = (params = {}) => {
  const qs = new URLSearchParams(params).toString();
  return request(`/episodes${qs ? '?' + qs : ''}`);
};
export const getEpisode = (id) => request(`/episodes/${id}`);
export const deleteEpisode = (id) => request(`/episodes/${id}`, { method: 'DELETE' });
export const translateEpisode = (id) =>
  request(`/episodes/${id}/translate`, { method: 'POST' });
export const transcribeEpisode = (id) =>
  request(`/episodes/${id}/transcribe`, { method: 'POST' });
export const digestEpisode = (id) =>
  request(`/episodes/${id}/digest`, { method: 'POST' });
export const runPipeline = (id) =>
  request(`/episodes/${id}/pipeline`, { method: 'POST' });
export const cleanupAudio = () =>
  request('/episodes/cleanup-audio', { method: 'POST' });
export const generateSynopsis = (id, { provider, model } = {}) =>
  request(`/episodes/${id}/synopsis`, { method: 'POST', body: JSON.stringify({ provider, model }) });
export const emailSynopsis = (id, { to, language }) =>
  request(`/episodes/${id}/synopsis/email`, { method: 'POST', body: JSON.stringify({ to, language }) });

// Batch pipeline triggers — one job is queued per eligible episode; the
// backend job runner executes them one at a time.
export const processPodcast = (id, step) =>
  request(`/podcasts/${id}/process/${step}`, { method: 'POST' });
export const processLibrary = (step) =>
  request(`/episodes/process/${step}`, { method: 'POST' });

// Transcripts
export const getTranscript = (episodeId) =>
  request(`/episodes/${episodeId}/transcript`);

// Who speaks in an episode; admins can rename a speaker (empty name: unnamed).
export const getSpeakers = (episodeId) => request(`/episodes/${episodeId}/speakers`);
export const renameSpeaker = (episodeId, label, name) =>
  request(`/episodes/${episodeId}/speakers/${encodeURIComponent(label)}`, {
    method: 'PUT',
    body: JSON.stringify({ name }),
  });

// An episode's transcript as Markdown.
export const downloadTranscript = (episodeId) =>
  download(`/episodes/${episodeId}/transcript/export`, {}, `transcript_${episodeId}.md`);

// A zip with one Markdown transcript per episode of a podcast.
export const exportPodcastTranscripts = (podcastId) =>
  download(
    `/podcasts/${podcastId}/transcripts/export`,
    { method: 'POST' },
    `podcast_${podcastId}_transcripts.zip`
  );

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
export const getBlog = (name) => request(`/blogs/${encodeURIComponent(name)}`);
export const createBlog = (data) =>
  request('/blogs', { method: 'POST', body: JSON.stringify(data) });

// LinkedIn posts
export const getLinkedInPosts = () => request('/linkedin');
export const getLinkedInPost = (name) => request(`/linkedin/${encodeURIComponent(name)}`);
export const createLinkedInPost = (data) =>
  request('/linkedin', { method: 'POST', body: JSON.stringify(data) });

// Search
export const search = (q) => request(`/search?${new URLSearchParams({ q })}`);

// Ask: questions answered from the library, streamed as Server-Sent Events.
// Calls onEvent with each event ({ event: 'status' | 'sources' | 'token' |
// 'done' | 'error', ... }) as it arrives.
// ``filters`` ({ podcast_ids, after, before }) replace any the question implies.
export async function askStream(question, threadId, onEvent, signal, filters = {}) {
  const res = await fetch(`${BASE}/ask`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, thread_id: threadId || undefined, ...filters }),
    signal,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `API error ${res.status}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let end;
      while ((end = buffer.indexOf('\n\n')) !== -1) {
        const block = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        const data = block.split('\n').find((line) => line.startsWith('data: '));
        if (data) onEvent(JSON.parse(data.slice(6)));
      }
    }
  } finally {
    // Also when onEvent throws: stop reading so the server sees the close.
    reader.cancel().catch(() => {});
  }
}
export const stopAsk = () => request('/ask/stop', { method: 'POST' });
export const getAskStatus = (library = false) =>
  request(`/ask/status${library ? '?library=true' : ''}`);
export const getAskThreads = () => request('/ask/threads');
export const getAskThread = (id) => request(`/ask/threads/${id}`);
export const deleteAskThread = (id) => request(`/ask/threads/${id}`, { method: 'DELETE' });
export const rateAskAnswer = (messageId, rating) =>
  request(`/ask/messages/${messageId}/rating`, {
    method: 'POST',
    body: JSON.stringify({ rating }),
  });
export const indexLibrary = () => request('/ask/index', { method: 'POST' });
export const getAskEval = () => request('/ask/eval');
export const generateAskQuestions = (count = 60) =>
  request(`/ask/eval/generate?count=${count}`, { method: 'POST' });
export const runAskEval = () => request('/ask/eval/run', { method: 'POST' });
export const getAskFeedback = () => request('/ask/feedback');
export const addFeedbackToEval = (messageId) =>
  request(`/ask/feedback/${messageId}/to-eval`, { method: 'POST' });

// Daily run
export const getDailyRun = () => request('/daily-run');

// Settings
export const getSettings = () => request('/settings');
export const updateSettings = (data) =>
  request('/settings', { method: 'PUT', body: JSON.stringify(data) });

// Sign-in (installs without a reverse proxy) and the setup wizard
export const getAuthStatus = () => request('/auth/status');
export const createFirstAdmin = (account) =>
  request('/auth/setup', { method: 'POST', body: JSON.stringify(account) });
export const login = (username, password) =>
  request('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) });
export const logout = () => request('/auth/logout', { method: 'POST' });
export const changeMyPassword = (current_password, new_password) =>
  request('/me/password', {
    method: 'POST',
    body: JSON.stringify({ current_password, new_password }),
  });
export const createUser = (user) =>
  request('/admin/users', { method: 'POST', body: JSON.stringify(user) });
export const resetPassword = (username, password) =>
  request(`/admin/users/${encodeURIComponent(username)}/password`, {
    method: 'PUT',
    body: JSON.stringify({ password }),
  });

export const getSetupProgress = () => request('/setup/progress');
export const finishSetup = () => request('/setup/progress', { method: 'POST' });
export const getSecrets = () => request('/setup/secrets');
export const setSecrets = (secrets) =>
  request('/setup/secrets', { method: 'PUT', body: JSON.stringify(secrets) });
export const checkOllama = () => request('/setup/ollama');
export const sendTestEmail = (to) =>
  request('/setup/test-email', { method: 'POST', body: JSON.stringify({ to }) });
export const getNetwork = () => request('/setup/network');
export const setNetwork = (lan, force = false) =>
  request('/setup/network', { method: 'PUT', body: JSON.stringify({ lan, force }) });
