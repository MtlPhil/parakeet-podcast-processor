import { useEffect, useState } from 'react';
import { getEpisodePosts } from '../api/client';

// The user's blog and LinkedIn posts made from an episode, newest first,
// with each blog post's grade per write -> grade -> revise round.
export default function EpisodePosts({ episodeId, language }) {
  const [posts, setPosts] = useState(null);
  const [open, setOpen] = useState(null);

  useEffect(() => {
    getEpisodePosts(episodeId)
      .then(setPosts)
      .catch(() => setPosts([]));
  }, [episodeId]);

  if (!posts) return <p className="text-gray-500 text-sm">Loading posts...</p>;
  if (!posts.length)
    return (
      <p className="text-gray-500 text-sm">
        No posts from this episode yet. Write a LinkedIn post from the Overview tab, or a blog post
        from the Content page.
      </p>
    );

  return (
    <ul className="space-y-3">
      {posts.map((p) => {
        const french = language === 'fr' && p.content_fr;
        return (
          <li key={`${p.kind}/${p.filename}`} className="border border-gray-200 rounded-lg p-3">
            <button
              onClick={() => setOpen(open === p.filename ? null : p.filename)}
              className="w-full text-left flex flex-wrap items-baseline gap-2"
              aria-expanded={open === p.filename}
            >
              <span className="text-xs uppercase tracking-wide text-gray-400">
                {p.kind === 'blog' ? 'Blog' : 'LinkedIn'}
              </span>
              <span className="font-medium text-gray-800">{p.title}</span>
              <span className="text-xs text-gray-500">{p.date}</span>
              {p.final_grade && (
                <span className="text-xs text-gray-600">
                  Grade {p.final_grade}
                  {p.final_score != null && ` (${p.final_score})`}
                  {p.grades && ` · rounds: ${p.grades}`}
                </span>
              )}
            </button>
            {open === p.filename && (
              <div className="prose prose-sm max-w-none text-gray-700 whitespace-pre-wrap mt-3">
                {french ? p.content_fr : p.content}
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}
