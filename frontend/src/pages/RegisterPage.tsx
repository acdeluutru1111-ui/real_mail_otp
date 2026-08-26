import { useState, type FormEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import ErrorBanner from '../components/ErrorBanner';

export function RegisterPage() {
  const { register } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<unknown>(null);
  const [submitting, setSubmitting] = useState(false);

  // Password must be <= 72 bytes (backend constraint).
  const passwordTooLong = new TextEncoder().encode(password).length > 72;

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (passwordTooLong) return;
    setError(null);
    setSubmitting(true);
    try {
      await register({ email, password });
      navigate('/inboxes', { replace: true });
    } catch (err) {
      setError(err);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="auth-card">
      <h1>Create account</h1>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />
      <form onSubmit={onSubmit}>
        <label>
          Email
          <input
            type="email"
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
        </label>
        <label>
          Password
          <input
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </label>
        {passwordTooLong && (
          <p className="field-error">Password must be at most 72 bytes.</p>
        )}
        <button
          type="submit"
          className="btn-primary"
          disabled={submitting || passwordTooLong}
        >
          {submitting ? 'Creating…' : 'Create account'}
        </button>
      </form>
      <p className="muted">
        Already have an account? <Link to="/login">Sign in</Link>
      </p>
    </div>
  );
}

export default RegisterPage;
