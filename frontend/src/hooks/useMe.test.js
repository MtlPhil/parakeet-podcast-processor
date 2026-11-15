import { describe, expect, it } from 'vitest';
import { languageOf } from './useMe';

const me = (language) => ({ settings: { language } });

describe('languageOf', () => {
  it('shows what was picked on the page first', () => {
    expect(languageOf(me('fr'), 'en')).toBe('en');
  });

  it('follows the setting, English by default', () => {
    expect(languageOf(me('fr'))).toBe('fr');
    expect(languageOf(me('en'))).toBe('en');
    expect(languageOf(null)).toBe('en');
    expect(languageOf(me(undefined))).toBe('en');
  });

  it("gives 'original' the episode's language, English unless French", () => {
    expect(languageOf(me('original'), null, 'fr')).toBe('fr');
    expect(languageOf(me('original'), null, 'es')).toBe('en');
    expect(languageOf(me('original'))).toBe('en');
  });
});
