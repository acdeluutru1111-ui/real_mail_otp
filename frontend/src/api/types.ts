// TypeScript types mirroring the backend API contract (see backend/openapi/openapi.yaml).
// The backend NEVER returns cookie / key / payload / raw OTP fields — do not model them.

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: 'bearer';
}

export interface AuthCredentials {
  email: string;
  password: string;
}

export interface RefreshRequest {
  refresh_token: string;
}

export type InboxStatus = 'active' | 'expired' | 'deleted';

export interface Inbox {
  id: string;
  address: string;
  domain_type: string;
  status: InboxStatus;
  created_at: string;
  expires_at: string | null;
}

export interface InboxList {
  items: Inbox[];
  next_cursor?: string | null;
}

export interface InboxCreateRequest {
  domain: string;
}

export interface MessageMeta {
  mid: string;
  subject: string;
  sender: string;
  snippet?: string;
  received_at: string;
}

export interface MessageList {
  items: MessageMeta[];
}

export interface RefreshResult {
  messages: MessageMeta[];
  next_poll_after_seconds: number;
  refreshed_at: string;
}

export type BillingSource = 'upstream' | 'cache';

export interface Billing {
  charged: boolean;
  amount: number;
  source: BillingSource;
}

export interface MessageDetail {
  mid: string;
  subject: string;
  sender: string;
  received_at: string;
  html_sanitized: string;
  otp_candidates?: string[];
  billing: Billing;
}

export interface Balance {
  balance_vnd: number;
}

export type LedgerType = 'credit' | 'debit' | 'reversal';

export interface LedgerEntry {
  id: string;
  type: LedgerType;
  amount_vnd: number;
  reference_type?: string;
  reference_id?: string;
  created_at: string;
}

export interface LedgerList {
  items: LedgerEntry[];
  next_cursor?: string | null;
}

export type PackageCode = 'starter' | 'popular' | 'pro';

export interface PaymentQrRequest {
  package_code?: PackageCode;
  amount_vnd?: number;
}

export interface ManualProofRequest {
  note: string;
  reference?: string;
}

export type PaymentStatus =
  | 'pending'
  | 'pending_review'
  | 'paid'
  | 'rejected'
  | 'expired';

export interface Payment {
  id: string;
  package_code?: string | null;
  amount_vnd: number;
  provider_ref: string;
  qr_content: string;
  status: PaymentStatus;
  created_at: string;
  paid_at?: string | null;
}

export interface ErrorEnvelope {
  error: {
    code: string;
    message: string;
    retryable: boolean;
  };
  request_id: string;
}

// Pagination options shared across list endpoints.
export interface PageQuery {
  cursor?: string;
  limit?: number;
}
