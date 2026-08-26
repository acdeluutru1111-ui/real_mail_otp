import { BrowserRouter } from 'react-router-dom';
import { AuthProvider } from './auth/AuthContext';
import AppRouter from './router';

export function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <AppRouter />
      </BrowserRouter>
    </AuthProvider>
  );
}

export default App;
