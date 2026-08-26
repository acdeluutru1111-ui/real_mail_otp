// Top navigation bar. Only shown for authenticated users.

import { NavLink, useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';

export function NavBar() {
  const { isAuthenticated, logout } = useAuth();
  const navigate = useNavigate();

  if (!isAuthenticated) return null;

  const handleLogout = () => {
    logout();
    navigate('/login', { replace: true });
  };

  return (
    <nav className="navbar">
      <div className="navbar-brand">📬 Real Mail OTP</div>
      <div className="navbar-links">
        <NavLink to="/inboxes">Inboxes</NavLink>
        <NavLink to="/billing">Billing</NavLink>
        <NavLink to="/payments">Payments</NavLink>
      </div>
      <button type="button" className="btn-link" onClick={handleLogout}>
        Log out
      </button>
    </nav>
  );
}

export default NavBar;
