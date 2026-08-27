// Presentational list of message metadata. Selecting a row navigates to detail.

import { Link } from 'react-router-dom';
import type { MessageMeta } from '../api/types';

export interface MessageListProps {
  inboxId: string;
  messages: MessageMeta[];
}

export function MessageList({ inboxId, messages }: MessageListProps) {
  if (messages.length === 0) {
    return <p className="muted">No messages yet.</p>;
  }
  return (
    <ul className="message-list">
      {messages.map((m) => (
        <li key={m.mid} className="message-list-item">
          <Link to={`/inboxes/${inboxId}/messages/${m.mid}`}>
            <div className="message-subject">{m.subject || '(no subject)'}</div>
            <div className="message-meta">
              <span className="message-sender">{m.sender}</span>
              <span className="message-date">
                {m.received_at ? new Date(m.received_at).toLocaleString() : ''}
              </span>
            </div>
            {m.snippet && <div className="message-snippet">{m.snippet}</div>}
          </Link>
        </li>
      ))}
    </ul>
  );
}

export default MessageList;
