import { describe, expect, it } from 'vitest';
import { sortBy } from './podcastSort';

const podcasts = [
  { title: 'beta Show', added_at: '2026-03-01T10:00:00', episode_count: 4 },
  { title: 'Alpha 10', added_at: '2026-01-01T10:00:00', episode_count: 9 },
  { title: 'Alpha 9', added_at: '2026-03-01T10:00:00', episode_count: 4 },
  { title: 'Ça va', added_at: null, episode_count: undefined },
];
const titles = (sort, reversed = false) =>
  [...podcasts].sort(sortBy(sort, reversed)).map((p) => p.title);

describe('sortBy', () => {
  it('sorts names A to Z ignoring case and accents, numbers by value', () => {
    expect(titles('name')).toEqual(['Alpha 9', 'Alpha 10', 'beta Show', 'Ça va']);
    expect(titles('name', true)).toEqual(['Ça va', 'beta Show', 'Alpha 10', 'Alpha 9']);
  });

  it('puts the newest first, ties by name, unknown dates last', () => {
    expect(titles('added')).toEqual(['Alpha 9', 'beta Show', 'Alpha 10', 'Ça va']);
  });

  it('reverses the order but keeps ties A to Z', () => {
    expect(titles('added', true)).toEqual(['Ça va', 'Alpha 10', 'Alpha 9', 'beta Show']);
    expect(titles('episodes', true)).toEqual(['Ça va', 'Alpha 9', 'beta Show', 'Alpha 10']);
  });

  it('puts the most episodes first, a missing count as none', () => {
    expect(titles('episodes')).toEqual(['Alpha 10', 'Alpha 9', 'beta Show', 'Ça va']);
  });
});
