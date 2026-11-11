import { createContext, useContext } from 'react';

// The signed-in user ({ username, role, settings, ... }), a reload() to call
// after changing their settings, and the install's sign-in mode: 'proxy'
// (accounts come from the reverse proxy), 'local' (P3's own accounts) or
// 'single' (one person, no sign-in).
// Provided by App.
export const MeContext = createContext({ me: null, reload: () => {}, authMode: 'proxy' });

export const useMe = () => useContext(MeContext);

// The language a page shows first: the one picked on the page, else the
// user's setting.
export const languageOf = (me, picked) => picked || me?.settings?.language || 'en';
