// Presentational ledger table for billing history.

import type { LedgerEntry } from '../api/types';

export interface LedgerTableProps {
  entries: LedgerEntry[];
}

function signFor(type: LedgerEntry['type']): string {
  if (type === 'credit') return '+';
  if (type === 'debit') return '−';
  return '±';
}

export function LedgerTable({ entries }: LedgerTableProps) {
  if (entries.length === 0) {
    return <p className="muted">No ledger entries yet.</p>;
  }
  return (
    <table className="data-table">
      <thead>
        <tr>
          <th>Type</th>
          <th>Amount (đ)</th>
          <th>Reference</th>
          <th>When</th>
        </tr>
      </thead>
      <tbody>
        {entries.map((e) => (
          <tr key={e.id}>
            <td>
              <span className={`ledger-type ledger-${e.type}`}>{e.type}</span>
            </td>
            <td className={`ledger-amount ledger-${e.type}`}>
              {signFor(e.type)}
              {e.amount_vnd.toLocaleString()}
            </td>
            <td>
              {e.reference_type ? (
                <span title={e.reference_id}>{e.reference_type}</span>
              ) : (
                '—'
              )}
            </td>
            <td>{new Date(e.created_at).toLocaleString()}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default LedgerTable;
