// In-memory token store. Tokens are kept in module-scoped variables and are
// intentionally NOT persisted to localStorage/sessionStorage/cookies, and are
// NEVER logged. Reloading the tab logs the user out (acceptable for this app).

import type { TokenPair } from '../api/types';

let accessToken: string | null = null;
let refreshToken: string | null = null;

// Simple subscriber mechanism so the auth context / router can react to changes
// (e.g. redirect to /login when tokens are cleared).
type Listener = () => void;
const listeners = new Set<Listener>();

function notify(): void {
  for (const l of listeners) l();
}

export function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getAccessToken(): string | null {
  return accessToken;
}

export function getRefreshToken(): string | null {
  return refreshToken;
}

export function isAuthenticated(): boolean {
  return accessToken !== null;
}

export function setTokens(pair: TokenPair): void {
  accessToken = pair.access_token;
  refreshToken = pair.refresh_token;
  notify();
}

export function clearTokens(): void {
  accessToken = null;
  refreshToken = null;
  notify();
}
