import { useCallback, useEffect, useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import { getInbox, listMessages, refreshInbox } from '../api/endpoints';
import type { Inbox, MessageMeta, RefreshResult } from '../api/types';
import { usePolling } from '../hooks/usePolling';
import { POLL_SCHEDULE_SECONDS } from '../security/polling';
import MessageList from '../components/MessageList';
import ErrorBanner from '../components/ErrorBanner';
import Spinner from '../components/Spinner';

export function InboxDetailPage() {
  const { id = '' } = useParams();

  const [inbox, setInbox] = useState<Inbox | null>(null);
  const [messages, setMessages] = useState<MessageMeta[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [countdown, setCountdown] = useState<number | null>(null);

  const knownMids = useMemo(
    () => new Set(messages.map((m) => m.mid)),
    [messages],
  );

  // Stop polling when a NEW message (not previously known) arrives.
  const matcher = useCallback(
    (result: RefreshResult) =>
      result.messages.some((m) => !knownMids.has(m.mid)),
    [knownMids],
  );

  const onRefresh = useCallback((result: RefreshResult) => {
    setMessages(result.messages);
  }, []);

  const { isPolling, schedule, lastRefreshedAt, stopReason, start, stop } =
    usePolling({ inboxId: id, matcher, onRefresh, onError: setError });

  const loadInbox = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [inboxRes, messagesRes] = await Promise.all([
        getInbox(id),
        listMessages(id),
      ]);
      setInbox(inboxRes);
      setMessages(messagesRes.items);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    void loadInbox();
    if (id) {
      start();
    }
  }, [loadInbox, id, start]);

  const handleManualRefresh = async () => {
    setRefreshing(true);
    setError(null);
    try {
      const res = await refreshInbox(id);
      if (res && res.messages) {
        setMessages(res.messages);
      }
    } catch (err) {
      setError(err);
    } finally {
      setRefreshing(false);
    }
  };

  // Live countdown to the next poll based on schedule.fireAt.
  useEffect(() => {
    if (!isPolling || !schedule) {
      setCountdown(null);
      return;
    }
    const tick = () => {
      const remaining = Math.max(0, Math.round((schedule.fireAt - Date.now()) / 1000));
      setCountdown(remaining);
    };
    tick();
    const t = setInterval(tick, 500);
    return () => clearInterval(t);
  }, [isPolling, schedule]);

  const stopLabel = (): string | null => {
    switch (stopReason) {
      case 'messages':
        return 'New mail arrived — polling stopped.';
      case 'exhausted':
        return 'No mail after 120s — polling stopped.';
      case 'error':
        return 'Polling stopped due to an error.';
      case 'manual':
        return 'Polling stopped.';
      default:
        return null;
    }
  };

  if (loading && !inbox) return <Spinner label="Loading inbox…" />;

  return (
    <div className="page">
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      {inbox && (
        <header className="inbox-header">
          <h1>{inbox.address}</h1>
          <div className="muted">
            {inbox.domain_type} · <span className={`status status-${inbox.status}`}>{inbox.status}</span>
          </div>
        </header>
      )}

      <section className="polling-panel">
        <h2>Wait for mail</h2>
        <p className="muted">
          Polls on this schedule (elapsed seconds):{' '}
          <code>[{POLL_SCHEDULE_SECONDS.join(', ')}]</code>. Stops on new mail,
          when you leave, on logout, or after 120s. Refreshing never charges.
        </p>

        <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'center', marginTop: '0.5rem' }}>
          {!isPolling ? (
            <button type="button" className="btn-primary" onClick={start}>
              Start waiting for mail
            </button>
          ) : (
            <button type="button" className="btn-secondary" onClick={stop}>
              Stop waiting
            </button>
          )}

          <button
            type="button"
            className="btn-secondary"
            disabled={refreshing}
            onClick={handleManualRefresh}
          >
            {refreshing ? 'Refreshing…' : '🔄 Refresh now'}
          </button>

          {isPolling && (
            <span className="polling-status">
              {countdown !== null ? `Next poll in ${countdown}s` : 'Polling…'}
              {schedule && ` · poll ${schedule.pollIndex + 1}/${schedule.totalPolls}`}
            </span>
          )}
        </div>

        {!isPolling && stopReason && (
          <p className="polling-stop-reason">{stopLabel()}</p>
        )}
        {lastRefreshedAt && (
          <p className="muted">
            Last refreshed: {new Date(lastRefreshedAt).toLocaleTimeString()}
          </p>
        )}
      </section>

      <section>
        <h2>Messages</h2>
        <MessageList inboxId={id} messages={messages} />
      </section>
    </div>
  );
}

export default InboxDetailPage;
