const LABELS = {
  youtube_channel: ['YouTube channel', 'bg-red-100 text-red-800'],
  youtube_video: ['YouTube video', 'bg-red-100 text-red-800'],
};

// Marks YouTube sources; RSS feeds, the default, get no badge.
export default function SourceBadge({ type }) {
  const entry = LABELS[type];
  if (!entry) return null;
  const [label, color] = entry;
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${color}`}>
      {label}
    </span>
  );
}
