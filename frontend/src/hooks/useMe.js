import { createContext, useContext } from 'react';

// The signed-in user ({ username, role, settings, ... }) and a reload()
// to call after changing their settings. Provided by App.
export const MeContext = createContext({ me: null, reload: () => {} });

export const useMe = () => useContext(MeContext);

// The language a page shows first: the one picked on the page, else the
// user's setting.
export const languageOf = (me, picked) => picked || me?.settings?.language || 'en';
