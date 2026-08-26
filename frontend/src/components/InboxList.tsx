// Presentational list of inboxes with select + delete actions.

import { Link } from 'react-router-dom';
import type { Inbox } from '../api/types';

export interface InboxListProps {
  inboxes: Inbox[];
  onDelete: (id: string) => void;
  deletingId?: string | null;
}

export function InboxList({ inboxes, onDelete, deletingId }: InboxListProps) {
  if (inboxes.length === 0) {
    return <p className="muted">No inboxes yet. Create one above.</p>;
  }
  return (
    <table className="data-table">
      <thead>
        <tr>
          <th>Address</th>
          <th>Type</th>
          <th>Status</th>
          <th>Created</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {inboxes.map((inbox) => (
          <tr key={inbox.id}>
            <td>
              <Link to={`/inboxes/${inbox.id}`}>{inbox.address}</Link>
            </td>
            <td>{inbox.domain_type}</td>
            <td>
              <span className={`status status-${inbox.status}`}>
                {inbox.status}
              </span>
            </td>
            <td>{new Date(inbox.created_at).toLocaleString()}</td>
            <td>
              <button
                type="button"
                className="btn-danger btn-sm"
                disabled={deletingId === inbox.id}
                onClick={() => onDelete(inbox.id)}
              >
                {deletingId === inbox.id ? 'Deleting…' : 'Delete'}
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default InboxList;
