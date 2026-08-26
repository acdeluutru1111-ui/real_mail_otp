// Typed functions for every endpoint in the API contract.

import { apiRequest } from './client';
import type {
  AuthCredentials,
  Balance,
  Inbox,
  InboxCreateRequest,
  InboxList,
  LedgerList,
  ManualProofRequest,
  MessageDetail,
  MessageList,
  PageQuery,
  Payment,
  PaymentQrRequest,
  RefreshResult,
  TokenPair,
} from './types';

// ---- Auth ----

export function register(creds: AuthCredentials): Promise<TokenPair> {
  return apiRequest<TokenPair>({
    method: 'POST',
    path: '/v1/auth/register',
    body: creds,
  });
}

export function login(creds: AuthCredentials): Promise<TokenPair> {
  return apiRequest<TokenPair>({
    method: 'POST',
    path: '/v1/auth/login',
    body: creds,
  });
}

// Note: token refresh on 401 is handled transparently inside client.ts.

// ---- Inboxes ----

export function createInbox(
  body: InboxCreateRequest,
  idempotencyKey?: string,
): Promise<Inbox> {
  return apiRequest<Inbox>({
    method: 'POST',
    path: '/v1/inboxes',
    body,
    headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined,
  });
}

export function listInboxes(page: PageQuery = {}): Promise<InboxList> {
  return apiRequest<InboxList>({
    method: 'GET',
    path: '/v1/inboxes',
    query: { cursor: page.cursor, limit: page.limit },
  });
}

export function getInbox(id: string): Promise<Inbox> {
  return apiRequest<Inbox>({ method: 'GET', path: `/v1/inboxes/${id}` });
}

export function deleteInbox(id: string): Promise<void> {
  return apiRequest<void>({ method: 'DELETE', path: `/v1/inboxes/${id}` });
}

// Refresh NEVER charges. May return 202 (in progress) -> RefreshResult may be undefined.
export function refreshInbox(
  id: string,
  signal?: AbortSignal,
): Promise<RefreshResult | undefined> {
  return apiRequest<RefreshResult | undefined>({
    method: 'POST',
    path: `/v1/inboxes/${id}/refresh`,
    signal,
  });
}

// ---- Messages ----

export function listMessages(inboxId: string): Promise<MessageList> {
  return apiRequest<MessageList>({
    method: 'GET',
    path: `/v1/inboxes/${inboxId}/messages`,
  });
}

// Charges only on a valid, committed read; reopen returns billing.charged=false.
export function getMessageDetail(
  inboxId: string,
  mid: string,
): Promise<MessageDetail> {
  return apiRequest<MessageDetail>({
    method: 'GET',
    path: `/v1/inboxes/${inboxId}/messages/${mid}`,
  });
}

// ---- Billing ----

export function getBalance(): Promise<Balance> {
  return apiRequest<Balance>({ method: 'GET', path: '/v1/billing/balance' });
}

export function getLedger(page: PageQuery = {}): Promise<LedgerList> {
  return apiRequest<LedgerList>({
    method: 'GET',
    path: '/v1/billing/ledger',
    query: { cursor: page.cursor, limit: page.limit },
  });
}

// ---- Payments ----

export function createPaymentQr(body: PaymentQrRequest): Promise<Payment> {
  return apiRequest<Payment>({
    method: 'POST',
    path: '/v1/payments/qr',
    body,
  });
}

export function submitManualProof(
  id: string,
  body: ManualProofRequest,
): Promise<Payment> {
  return apiRequest<Payment>({
    method: 'POST',
    path: `/v1/payments/${id}/manual-proof`,
    body,
  });
}

export function getPayment(id: string): Promise<Payment> {
  return apiRequest<Payment>({ method: 'GET', path: `/v1/payments/${id}` });
}
