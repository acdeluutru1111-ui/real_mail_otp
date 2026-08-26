import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import {
  createPaymentQr,
  getPayment,
  submitManualProof,
} from '../api/endpoints';
import type { PackageCode, Payment } from '../api/types';
import ErrorBanner from '../components/ErrorBanner';
import { PACKAGES, formatVnd } from '../lib/packages';

// Poll payment status every few seconds until it reaches a terminal state.
const PAYMENT_POLL_MS = 5000;
const TERMINAL: Payment['status'][] = ['paid', 'rejected', 'expired'];

export function PaymentsPage() {
  const [payment, setPayment] = useState<Payment | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [creating, setCreating] = useState(false);

  // Create form
  const [selectedPackage, setSelectedPackage] = useState<PackageCode | ''>(
    'starter',
  );
  const [customAmount, setCustomAmount] = useState('');

  // Manual proof form
  const [note, setNote] = useState('');
  const [reference, setReference] = useState('');
  const [submittingProof, setSubmittingProof] = useState(false);

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const refreshPayment = useCallback(async (paymentId: string) => {
    try {
      const p = await getPayment(paymentId);
      setPayment(p);
      if (TERMINAL.includes(p.status)) {
        stopPolling();
      }
    } catch (err) {
      setError(err);
    }
  }, [stopPolling]);

  // Manage the status-polling interval for the active payment.
  useEffect(() => {
    stopPolling();
    if (payment && !TERMINAL.includes(payment.status)) {
      pollRef.current = setInterval(() => {
        void refreshPayment(payment.id);
      }, PAYMENT_POLL_MS);
    }
    return stopPolling;
  }, [payment, refreshPayment, stopPolling]);

  const onCreate = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setCreating(true);
    try {
      const body =
        selectedPackage === ''
          ? { amount_vnd: Number(customAmount) }
          : { package_code: selectedPackage };
      const p = await createPaymentQr(body);
      setPayment(p);
    } catch (err) {
      setError(err);
    } finally {
      setCreating(false);
    }
  };

  const onSubmitProof = async (e: FormEvent) => {
    e.preventDefault();
    if (!payment) return;
    setError(null);
    setSubmittingProof(true);
    try {
      const p = await submitManualProof(payment.id, {
        note,
        reference: reference || undefined,
      });
      setPayment(p);
      setNote('');
      setReference('');
    } catch (err) {
      setError(err);
    } finally {
      setSubmittingProof(false);
    }
  };

  return (
    <div className="page">
      <h1>Payments</h1>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <section>
        <h2>Packages</h2>
        <table className="data-table">
          <thead>
            <tr>
              <th>Package</th>
              <th>Top-up</th>
              <th>Reads</th>
              <th>Per read</th>
            </tr>
          </thead>
          <tbody>
            {PACKAGES.map((p) => (
              <tr key={p.code}>
                <td>{p.label}</td>
                <td>{p.topupVnd === null ? 'Any' : formatVnd(p.topupVnd)}</td>
                <td>
                  {p.reads === null ? `${formatVnd(p.perReadVnd)} = 1 read` : p.reads}
                </td>
                <td>~{formatVnd(p.perReadVnd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section>
        <h2>Create a top-up</h2>
        <form className="stacked-form" onSubmit={onCreate}>
          <label>
            Package
            <select
              value={selectedPackage}
              onChange={(e) =>
                setSelectedPackage(e.target.value as PackageCode | '')
              }
            >
              <option value="starter">Starter (19,000đ)</option>
              <option value="popular">Popular (29,000đ)</option>
              <option value="pro">Pro (49,000đ)</option>
              <option value="">Pay as you go (custom amount)</option>
            </select>
          </label>
          {selectedPackage === '' && (
            <label>
              Amount (đ)
              <input
                type="number"
                min={1}
                value={customAmount}
                onChange={(e) => setCustomAmount(e.target.value)}
                required
              />
            </label>
          )}
          <button type="submit" className="btn-primary" disabled={creating}>
            {creating ? 'Creating…' : 'Create QR'}
          </button>
        </form>
      </section>

      {payment && (
        <section className="payment-panel">
          <h2>Payment</h2>
          <dl className="payment-meta">
            <dt>Status</dt>
            <dd>
              <span className={`status status-${payment.status}`}>
                {payment.status}
              </span>
            </dd>
            <dt>Amount</dt>
            <dd>{formatVnd(payment.amount_vnd)}</dd>
            <dt>Provider ref</dt>
            <dd>
              <code>{payment.provider_ref}</code>
            </dd>
          </dl>

          {payment.qr_content && (
            <div className="qr-block">
              <span className="muted">QR content (scan / copy):</span>
              <pre className="qr-content">{payment.qr_content}</pre>
            </div>
          )}

          {!TERMINAL.includes(payment.status) && (
            <form className="stacked-form" onSubmit={onSubmitProof}>
              <h3>Submit manual proof</h3>
              <label>
                Note
                <textarea
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  required
                />
              </label>
              <label>
                Reference (optional)
                <input
                  value={reference}
                  onChange={(e) => setReference(e.target.value)}
                />
              </label>
              <button
                type="submit"
                className="btn-secondary"
                disabled={submittingProof}
              >
                {submittingProof ? 'Submitting…' : 'Submit proof'}
              </button>
            </form>
          )}
        </section>
      )}
    </div>
  );
}

export default PaymentsPage;
