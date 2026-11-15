const byName = (a, b) =>
  a.title.localeCompare(b.title, undefined, { sensitivity: 'base', numeric: true });

// Each sort's natural order first (A to Z, newest, most episodes), then
// reversed by the arrow button. Ties go by name, A to Z.
export const SORTS = {
  name: { label: 'Name', orders: ['A to Z', 'Z to A'], compare: () => 0 },
  added: {
    label: 'Date added',
    orders: ['Newest first', 'Oldest first'],
    compare: (a, b) => (b.added_at || '').localeCompare(a.added_at || ''),
  },
  episodes: {
    label: 'Episodes',
    orders: ['Most first', 'Fewest first'],
    compare: (a, b) => (b.episode_count ?? 0) - (a.episode_count ?? 0),
  },
};

export const sortBy = (sort, reversed) => (a, b) => {
  const sign = reversed ? -1 : 1;
  return sort === 'name'
    ? sign * byName(a, b)
    : sign * SORTS[sort].compare(a, b) || byName(a, b);
};
