import { useState } from 'react';
import { formatTags, parseTags } from '../tags';

const CHIP = 'bg-gray-100 text-gray-600 px-2 py-0.5 rounded';

export function TagChips({ value, className = CHIP }) {
  return parseTags(value).map((tag) => (
    <span key={tag} className={className}>{tag}</span>
  ));
}

// Type a tag and press Enter or comma to add it; Backspace on an empty field
// removes the last one. `value` and `onChange` use the comma-separated string.
export function TagInput({ value, onChange, placeholder }) {
  const [draft, setDraft] = useState('');
  const tags = parseTags(value);

  const commit = (text) => {
    onChange(formatTags(parseTags([...tags, text].join(','))));
    setDraft('');
  };

  const handleChange = (e) => {
    if (e.target.value.includes(',')) commit(e.target.value);
    else setDraft(e.target.value);
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      if (draft.trim()) commit(draft);
    } else if (e.key === 'Backspace' && !draft && tags.length) {
      onChange(formatTags(tags.slice(0, -1)));
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-1.5 w-full border rounded-lg px-2 py-1.5 text-sm focus-within:ring-2 focus-within:ring-blue-500">
      {tags.map((tag) => (
        <span key={tag} className="inline-flex items-center gap-1 bg-blue-50 text-blue-700 px-2 py-0.5 rounded">
          {tag}
          <button
            type="button"
            onClick={() => onChange(formatTags(tags.filter((t) => t !== tag)))}
            aria-label={`Remove ${tag}`}
            className="text-blue-400 hover:text-blue-700"
          >
            ×
          </button>
        </span>
      ))}
      <input
        type="text"
        value={draft}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        onBlur={() => draft.trim() && commit(draft)}
        placeholder={tags.length ? '' : placeholder}
        className="flex-1 min-w-[8rem] py-0.5 outline-none bg-transparent"
      />
    </div>
  );
}
