import { createContext, useContext } from 'react';

// The signed-in user ({ username, role, settings, ... }), a reload() to call
// after changing their settings, and the install's sign-in mode: 'proxy'
// (accounts come from the reverse proxy), 'local' (P3's own accounts) or
// 'single' (one person, no sign-in).
// Provided by App.
export const MeContext = createContext({ me: null, reload: () => {}, authMode: 'proxy' });

export const useMe = () => useContext(MeContext);

// The language ('en' or 'fr') a page shows first: the one picked on the
// page, else the user's setting. 'original' is the episode's own language:
// French for a French episode, English for any other (digests exist in those
// two only), and English where there is no single episode.
export const languageOf = (me, picked, episodeLanguage) => {
  if (picked) return picked;
  const setting = me?.settings?.language;
  if (setting === 'original') return episodeLanguage === 'fr' ? 'fr' : 'en';
  return setting === 'fr' ? 'fr' : 'en';
};
