import { describe, expect, it } from 'vitest';
import { formatTags, parseTags } from './tags';

describe('tags', () => {
  it('splits, trims and drops empty tags and repeats, whatever the case', () => {
    expect(parseTags(' Tech, news,,tech , News ,Science')).toEqual(['Tech', 'news', 'Science']);
  });

  it('reads no category as no tags', () => {
    expect(parseTags(null)).toEqual([]);
    expect(parseTags('')).toEqual([]);
  });

  it('writes them back as one string', () => {
    expect(formatTags(parseTags('a, b'))).toBe('a, b');
  });
});
