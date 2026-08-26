import { Navigate, Route, Routes } from 'react-router-dom';
import Protected from './components/Protected';
import NavBar from './components/NavBar';
import LoginPage from './pages/LoginPage';
import RegisterPage from './pages/RegisterPage';
import InboxesPage from './pages/InboxesPage';
import InboxDetailPage from './pages/InboxDetailPage';
import MessageDetailPage from './pages/MessageDetailPage';
import BillingPage from './pages/BillingPage';
import PaymentsPage from './pages/PaymentsPage';

export function AppRouter() {
  return (
    <>
      <NavBar />
      <main className="app-main">
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/register" element={<RegisterPage />} />

          <Route
            path="/inboxes"
            element={
              <Protected>
                <InboxesPage />
              </Protected>
            }
          />
          <Route
            path="/inboxes/:id"
            element={
              <Protected>
                <InboxDetailPage />
              </Protected>
            }
          />
          <Route
            path="/inboxes/:id/messages/:mid"
            element={
              <Protected>
                <MessageDetailPage />
              </Protected>
            }
          />
          <Route
            path="/billing"
            element={
              <Protected>
                <BillingPage />
              </Protected>
            }
          />
          <Route
            path="/payments"
            element={
              <Protected>
                <PaymentsPage />
              </Protected>
            }
          />

          <Route path="/" element={<Navigate to="/inboxes" replace />} />
          <Route path="*" element={<Navigate to="/inboxes" replace />} />
        </Routes>
      </main>
    </>
  );
}

export default AppRouter;
