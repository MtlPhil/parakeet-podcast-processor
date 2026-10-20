// A podcast's tags are stored as one comma-separated string (its `category`).
export const parseTags = (value) => {
  const seen = new Set();
  return (value || '')
    .split(',')
    .map((tag) => tag.trim())
    .filter((tag) => {
      const key = tag.toLowerCase();
      if (!tag || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
};

export const formatTags = (tags) => tags.join(', ');
