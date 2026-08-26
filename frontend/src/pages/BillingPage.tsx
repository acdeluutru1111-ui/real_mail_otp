import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { getBalance, getLedger } from '../api/endpoints';
import type { LedgerEntry } from '../api/types';
import LedgerTable from '../components/LedgerTable';
import ErrorBanner from '../components/ErrorBanner';
import Spinner from '../components/Spinner';
import { formatVnd } from '../lib/packages';

export function BillingPage() {
  const [balance, setBalance] = useState<number | null>(null);
  const [entries, setEntries] = useState<LedgerEntry[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const loadLedger = useCallback(async (cursor?: string) => {
    setLoading(true);
    setError(null);
    try {
      const res = await getLedger({ cursor });
      setEntries((prev) => (cursor ? [...prev, ...res.items] : res.items));
      setNextCursor(res.next_cursor ?? null);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const bal = await getBalance();
        setBalance(bal.balance_vnd);
      } catch (err) {
        setError(err);
      }
    })();
    void loadLedger();
  }, [loadLedger]);

  return (
    <div className="page">
      <h1>Billing</h1>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <div className="balance-card">
        <span className="balance-label">Balance</span>
        <span className="balance-value">
          {balance === null ? '—' : formatVnd(balance)}
        </span>
        <Link to="/payments" className="btn-primary">
          Top up
        </Link>
      </div>

      <h2>Ledger</h2>
      {loading && entries.length === 0 ? (
        <Spinner label="Loading ledger…" />
      ) : (
        <LedgerTable entries={entries} />
      )}

      {nextCursor && (
        <button
          type="button"
          className="btn-secondary"
          disabled={loading}
          onClick={() => loadLedger(nextCursor)}
        >
          {loading ? 'Loading…' : 'Load more'}
        </button>
      )}
    </div>
  );
}

export default BillingPage;
