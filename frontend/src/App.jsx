import {
  BrowserRouter,
  Navigate,
  Outlet,
  Route,
  Routes,
} from 'react-router-dom'
import Assistant from './pages/Assistant'
import Dashboard from './pages/Dashboard'
import DashboardLayout from './components/layout/DashboardLayout'
import Conversations from './pages/Conversations'

import Login from './pages/Login'
import Signup from './pages/Signup'

import { useAuth } from './context/AuthContext'

function ProtectedRoute() {
  const { isAuthenticated, loading } = useAuth()

  if (loading) {
    return (
      <div className="grid min-h-screen place-items-center bg-[#0b0d14] text-slate-300">
        Loading your workspace...
      </div>
    )
  }

  return isAuthenticated
    ? <Outlet />
    : <Navigate to="/login" replace />
}

function PublicRoute({ children }) {
  const { isAuthenticated, loading } = useAuth()

  if (loading) {
    return (
      <div className="grid min-h-screen place-items-center bg-[#0b0d14] text-slate-300">
        Loading...
      </div>
    )
  }

  return isAuthenticated
    ? <Navigate to="/dashboard" replace />
    : children
}

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Navigate to="/dashboard" replace />} />

        {/* Protected dashboard routes */}
        <Route element={<ProtectedRoute />}>
          <Route
            path="/dashboard"
            element={<DashboardLayout />}
          >
            <Route index element={<Dashboard />} />
            <Route path="assistant" element={<Assistant />} />
            <Route path="conversations" element={<Conversations />} />
          </Route>
        </Route>

        {/* Public authentication routes */}
        <Route
          path="/login"
          element={
            <PublicRoute>
              <Login />
            </PublicRoute>
          }
        />

        <Route
          path="/signup"
          element={
            <PublicRoute>
              <Signup />
            </PublicRoute>
          }
        />

        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}

export default App
