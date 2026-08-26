// Fetch-based API client.
// Responsibilities:
//  - Set JSON headers, inject the bearer token for all /v1 calls except /v1/auth/*.
//  - Parse the error envelope {error:{code,message,retryable}, request_id} and
//    throw a typed ApiError carrying status, code, message, retryable, request_id
//    and retryAfterSeconds (from the Retry-After header).
//  - On a 401 from a protected call, refresh the token pair once and retry once.
//  - Never log tokens.

import type { ErrorEnvelope, TokenPair } from './types';
import {
  getAccessToken,
  getRefreshToken,
  setTokens,
  clearTokens,
} from '../security/tokenStore';

const BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/+$/, '');

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryable: boolean;
  readonly requestId?: string;
  readonly retryAfterSeconds?: number;

  constructor(params: {
    status: number;
    code: string;
    message: string;
    retryable: boolean;
    requestId?: string;
    retryAfterSeconds?: number;
  }) {
    super(params.message);
    this.name = 'ApiError';
    this.status = params.status;
    this.code = params.code;
    this.retryable = params.retryable;
    this.requestId = params.requestId;
    this.retryAfterSeconds = params.retryAfterSeconds;
  }
}

export interface RequestOptions {
  method?: string;
  path: string; // e.g. '/v1/inboxes'
  body?: unknown;
  headers?: Record<string, string>;
  query?: Record<string, string | number | undefined>;
  signal?: AbortSignal;
  // Internal flag to avoid infinite refresh loops.
  _isRetry?: boolean;
}

function isAuthPath(path: string): boolean {
  return path.startsWith('/v1/auth/');
}

function buildUrl(path: string, query?: RequestOptions['query']): string {
  const url = new URL(BASE_URL + path, BASE_URL || window.location.origin);
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== '') {
        url.searchParams.set(k, String(v));
      }
    }
  }
  return url.toString();
}

function parseRetryAfter(res: Response): number | undefined {
  const raw = res.headers.get('Retry-After');
  if (!raw) return undefined;
  const seconds = Number(raw);
  if (!Number.isNaN(seconds)) return seconds;
  // HTTP-date form: compute delta from now.
  const when = Date.parse(raw);
  if (!Number.isNaN(when)) {
    return Math.max(0, Math.round((when - Date.now()) / 1000));
  }
  return undefined;
}

async function toApiError(res: Response): Promise<ApiError> {
  let code = 'unknown';
  let message = res.statusText || 'Request failed';
  let retryable = res.status >= 500 || res.status === 429;
  let requestId: string | undefined;
  try {
    const data = (await res.json()) as Partial<ErrorEnvelope>;
    if (data && data.error) {
      code = data.error.code ?? code;
      message = data.error.message ?? message;
      retryable = data.error.retryable ?? retryable;
      requestId = data.request_id;
    }
  } catch {
    // Non-JSON error body; keep defaults.
  }
  return new ApiError({
    status: res.status,
    code,
    message,
    retryable,
    requestId,
    retryAfterSeconds: parseRetryAfter(res),
  });
}

// Perform a single POST /v1/auth/refresh with the current refresh token.
// Returns true on success (tokens rotated), false otherwise.
async function tryRefreshTokens(): Promise<boolean> {
  const rt = getRefreshToken();
  if (!rt) return false;
  try {
    const res = await fetch(buildUrl('/v1/auth/refresh'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ refresh_token: rt }),
    });
    if (!res.ok) return false;
    const pair = (await res.json()) as TokenPair;
    setTokens(pair);
    return true;
  } catch {
    return false;
  }
}

export async function apiRequest<T>(opts: RequestOptions): Promise<T> {
  const { method = 'GET', path, body, headers = {}, query, signal } = opts;

  const finalHeaders: Record<string, string> = {
    Accept: 'application/json',
    ...headers,
  };

  if (body !== undefined && !(body instanceof FormData)) {
    finalHeaders['Content-Type'] = 'application/json';
  }

  if (!isAuthPath(path)) {
    const token = getAccessToken();
    if (token) finalHeaders['Authorization'] = `Bearer ${token}`;
  }

  const res = await fetch(buildUrl(path, query), {
    method,
    headers: finalHeaders,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });

  // Handle 401 on protected calls: refresh once, retry once.
  if (res.status === 401 && !isAuthPath(path) && !opts._isRetry) {
    const refreshed = await tryRefreshTokens();
    if (refreshed) {
      return apiRequest<T>({ ...opts, _isRetry: true });
    }
    clearTokens();
    throw await toApiError(res);
  }

  if (!res.ok) {
    throw await toApiError(res);
  }

  if (res.status === 204) {
    return undefined as T;
  }

  // 202 Accepted (refresh already in progress) may have no body.
  const text = await res.text();
  if (!text) return undefined as T;
  return JSON.parse(text) as T;
}
