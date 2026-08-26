// Displays a friendly error message from an ApiError (or any error), mapping
// known HTTP statuses to helpful copy while still surfacing the server message
// and request_id for support.

import { ApiError } from '../api/client';

export interface ErrorBannerProps {
  error: unknown;
  onDismiss?: () => void;
}

const STATUS_HINTS: Record<number, string> = {
  401: 'You need to sign in again.',
  402: 'Insufficient balance — top up to read this message.',
  403: 'You do not have access to this resource.',
  404: 'Not found.',
  409: 'Conflict — this action could not be completed.',
  429: 'Too many requests — please slow down and try again shortly.',
  502: 'The mail provider returned an invalid response. Try again.',
  503: 'The mail provider is temporarily unavailable. Try again.',
  504: 'The mail provider timed out. Try again.',
};

export function friendlyMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const hint = STATUS_HINTS[error.status];
    if (hint) return `${hint}${error.message ? ` (${error.message})` : ''}`;
    return error.message || 'Something went wrong.';
  }
  if (error instanceof Error) return error.message;
  return 'Something went wrong.';
}

export function ErrorBanner({ error, onDismiss }: ErrorBannerProps) {
  if (!error) return null;
  const requestId = error instanceof ApiError ? error.requestId : undefined;
  return (
    <div className="error-banner" role="alert">
      <span>{friendlyMessage(error)}</span>
      {requestId && <code className="error-request-id">#{requestId}</code>}
      {onDismiss && (
        <button type="button" className="error-dismiss" onClick={onDismiss}>
          ✕
        </button>
      )}
    </div>
  );
}

export default ErrorBanner;
