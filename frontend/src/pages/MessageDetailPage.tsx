import { useCallback, useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { getMessageDetail } from '../api/endpoints';
import type { MessageDetail } from '../api/types';
import { ApiError } from '../api/client';
import SanitizedHtml from '../security/SanitizedHtml';
import ErrorBanner from '../components/ErrorBanner';
import Spinner from '../components/Spinner';
import { formatVnd } from '../lib/packages';

export function MessageDetailPage() {
  const { id = '', mid = '' } = useParams();
  const navigate = useNavigate();

  const [detail, setDetail] = useState<MessageDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // Fetching detail may charge (a valid, committed read); a reopen returns
      // billing.charged = false.
      const res = await getMessageDetail(id, mid);
      setDetail(res);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [id, mid]);

  useEffect(() => {
    void load();
  }, [load]);

  const insufficientBalance =
    error instanceof ApiError && error.status === 402;

  if (loading) return <Spinner label="Opening message…" />;

  return (
    <div className="page">
      <Link to={`/inboxes/${id}`} className="back-link">
        ← Back to inbox
      </Link>

      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      {insufficientBalance && (
        <div className="topup-prompt">
          <p>You don’t have enough balance to open this message.</p>
          <button
            type="button"
            className="btn-primary"
            onClick={() => navigate('/payments')}
          >
            Top up now
          </button>
        </div>
      )}

      {detail && (
        <article className="message-detail">
          <header>
            <h1>{detail.subject || '(no subject)'}</h1>
            <div className="muted">
              From <strong>{detail.sender}</strong> ·{' '}
              {new Date(detail.received_at).toLocaleString()}
            </div>

            <div className="billing-indicator">
              {detail.billing.charged ? (
                <span className="billed">
                  Charged {formatVnd(detail.billing.amount)} (
                  {detail.billing.source})
                </span>
              ) : (
                <span className="reopened">Reopened (no charge)</span>
              )}
            </div>
          </header>

          {detail.otp_candidates && detail.otp_candidates.length > 0 && (
            <div className="otp-candidates">
              <span className="otp-label">OTP:</span>
              {detail.otp_candidates.map((code) => (
                <code key={code} className="otp-code">
                  {code}
                </code>
              ))}
            </div>
          )}

          <SanitizedHtml
            html={detail.html_sanitized}
            title={`Message ${detail.mid}`}
          />
        </article>
      )}
    </div>
  );
}

export default MessageDetailPage;
