// Auth context: exposes authentication state derived from the in-memory token
// store and helpers to log in / register / log out. The token store is the
// single source of truth; this context just mirrors it for React and triggers
// re-renders (e.g. so Protected routes redirect on logout).

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import {
  clearTokens,
  isAuthenticated as storeIsAuthenticated,
  setTokens,
  subscribe,
} from '../security/tokenStore';
import { login as apiLogin, register as apiRegister } from '../api/endpoints';
import type { AuthCredentials } from '../api/types';

interface AuthContextValue {
  isAuthenticated: boolean;
  login: (creds: AuthCredentials) => Promise<void>;
  register: (creds: AuthCredentials) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [authed, setAuthed] = useState<boolean>(storeIsAuthenticated());

  // Keep in sync with the token store (covers the transparent refresh-failure
  // path in the API client, which clears tokens directly).
  useEffect(() => {
    return subscribe(() => setAuthed(storeIsAuthenticated()));
  }, []);

  const login = useCallback(async (creds: AuthCredentials) => {
    const pair = await apiLogin(creds);
    setTokens(pair);
  }, []);

  const register = useCallback(async (creds: AuthCredentials) => {
    const pair = await apiRegister(creds);
    setTokens(pair);
  }, []);

  const logout = useCallback(() => {
    clearTokens();
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ isAuthenticated: authed, login, register, logout }),
    [authed, login, register, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider');
  return ctx;
}
