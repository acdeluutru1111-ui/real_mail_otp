import { useCallback, useEffect, useState, type FormEvent } from 'react';
import {
  createInbox,
  deleteInbox,
  listInboxes,
} from '../api/endpoints';
import type { Inbox } from '../api/types';
import InboxList from '../components/InboxList';
import ErrorBanner from '../components/ErrorBanner';
import Spinner from '../components/Spinner';

export function InboxesPage() {
  const [inboxes, setInboxes] = useState<Inbox[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const [domain, setDomain] = useState('outlook');
  const [creating, setCreating] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const load = useCallback(async (cursor?: string) => {
    setLoading(true);
    setError(null);
    try {
      const res = await listInboxes({ cursor });
      setInboxes((prev) => (cursor ? [...prev, ...res.items] : res.items));
      setNextCursor(res.next_cursor ?? null);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const onCreate = async (e: FormEvent) => {
    e.preventDefault();
    setCreating(true);
    setError(null);
    try {
      // Idempotency-Key so a retry does not create duplicate inboxes.
      const idempotencyKey = crypto.randomUUID();
      const inbox = await createInbox({ domain }, idempotencyKey);
      setInboxes((prev) => [inbox, ...prev.filter((i) => i.id !== inbox.id)]);
    } catch (err) {
      setError(err);
    } finally {
      setCreating(false);
    }
  };

  const onDelete = async (id: string) => {
    setDeletingId(id);
    setError(null);
    try {
      await deleteInbox(id);
      // Soft-delete: reflect status locally.
      setInboxes((prev) =>
        prev.map((i) => (i.id === id ? { ...i, status: 'deleted' } : i)),
      );
    } catch (err) {
      setError(err);
    } finally {
      setDeletingId(null);
    }
  };

  return (
    <div className="page">
      <h1>Inboxes</h1>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <form className="inline-form" onSubmit={onCreate}>
        <label>
          Domain
          <input
            value={domain}
            onChange={(e) => setDomain(e.target.value)}
            placeholder="outlook"
            required
          />
        </label>
        <button type="submit" className="btn-primary" disabled={creating}>
          {creating ? 'Creating…' : 'Create inbox'}
        </button>
      </form>

      {loading && inboxes.length === 0 ? (
        <Spinner label="Loading inboxes…" />
      ) : (
        <InboxList
          inboxes={inboxes}
          onDelete={onDelete}
          deletingId={deletingId}
        />
      )}

      {nextCursor && (
        <button
          type="button"
          className="btn-secondary"
          disabled={loading}
          onClick={() => load(nextCursor)}
        >
          {loading ? 'Loading…' : 'Load more'}
        </button>
      )}
    </div>
  );
}

export default InboxesPage;
