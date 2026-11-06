import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Link } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { getOAuthRedirectTo } from '../lib/authRedirect'
import { supabase } from '../lib/supabase'
import { withAuthTimeout } from '../lib/requestTimeout'

function Login() {
  const navigate = useNavigate()
  const { signIn } = useAuth()

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [theme, setTheme] = useState('dark')
  const [loading, setLoading] = useState(false)
  const [googleLoading, setGoogleLoading] = useState(false)
  const [message, setMessage] = useState(null)

  const isDark = theme === 'dark'

  async function handleLogin(event) {
    event.preventDefault()
    setMessage(null)
    setLoading(true)

    try {
      await signIn({ email, password })
      navigate('/dashboard', { replace: true })
    } catch (error) {
      const isInvalidCredentials =
        error.code === 'invalid_credentials' ||
        error.message?.toLowerCase().includes('invalid login credentials')

      setMessage({
        type: 'error',
        text: isInvalidCredentials
        ? "Incorrect email or password. Please check your credentials and try again. If you forgot your password, use the Forgot password option."
        : error.message || 'Unable to sign in. Please try again.',
      })
    } finally {
      setLoading(false)
    }
  }

  async function handleGoogleLogin() {
    setMessage(null)
    setGoogleLoading(true)

    try {
      const { error } = await withAuthTimeout(
        supabase.auth.signInWithOAuth({
          provider: 'google',
          options: {
            redirectTo: getOAuthRedirectTo(),
          },
        }),
        'Google sign-in',
      )

      if (error) throw error
    } catch (error) {
      setMessage({
        type: 'error',
        text: error.message || 'Google sign-in failed.',
      })
    } finally {
      setGoogleLoading(false)
    }
  }

  async function handleForgotPassword() {
    if (!email.trim()) {
      setMessage({
        type: 'error',
        text: 'Enter your email address first.',
      })
      return
    }

    setMessage(null)
    setLoading(true)

    try {
      const { error } = await withAuthTimeout(
        supabase.auth.resetPasswordForEmail(
          email.trim(),
          {
            redirectTo: `${window.location.origin}/login`,
          },
        ),
        'Password reset',
      )

      if (error) throw error

      setMessage({
        type: 'success',
        text: 'If an account exists for this email, a password recovery link will be sent.',
      })
    } catch (error) {
      setMessage({
        type: 'error',
        text: error.message || 'Unable to send recovery email.',
      })
    } finally {
      setLoading(false)
    }
  }

  return (
    <div
      className={`min-h-screen overflow-x-hidden transition-colors duration-300 ${
        isDark
          ? 'bg-[#080b14] text-white'
          : 'bg-slate-50 text-slate-900'
      }`}
    >
      {/* Background decoration */}
      <div className="pointer-events-none fixed inset-0 overflow-hidden">
        <div
          className={`absolute -left-40 -top-40 h-96 w-96 rounded-full blur-[120px] ${
            isDark ? 'bg-violet-700/20' : 'bg-violet-300/40'
          }`}
        />
        <div
          className={`absolute -bottom-40 right-0 h-96 w-96 rounded-full blur-[120px] ${
            isDark ? 'bg-blue-700/15' : 'bg-blue-300/30'
          }`}
        />
      </div>

      {/* Header */}
      <header className="relative z-10 flex items-center justify-between px-5 py-5 sm:px-8 lg:px-12">
        <Link to="/login" className="flex items-center gap-3">
          <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-gradient-to-br from-violet-500 to-indigo-600 text-2xl font-bold shadow-lg shadow-violet-500/20">
            ✧
          </div>

          <div>
            <h1 className="text-lg font-bold tracking-tight">
              Personal AI
            </h1>
            <p
              className={`text-xs ${
                isDark ? 'text-slate-400' : 'text-slate-500'
              }`}
            >
              Intelligent workspace
            </p>
          </div>
        </Link>

        <button
          type="button"
          onClick={() =>
            setTheme((current) =>
              current === 'dark' ? 'light' : 'dark'
            )
          }
          className={`rounded-full border px-4 py-2 text-sm font-medium transition hover:-translate-y-0.5 ${
            isDark
              ? 'border-slate-700 bg-slate-900/80 text-slate-200 hover:bg-slate-800'
              : 'border-slate-200 bg-white text-slate-700 shadow-sm hover:bg-slate-100'
          }`}
        >
          {isDark ? '☀ Light mode' : '☾ Dark mode'}
        </button>
      </header>

      {/* Main */}
      <main className="relative z-10 mx-auto grid min-h-[calc(100vh-85px)] max-w-7xl grid-cols-1 items-center gap-12 px-5 pb-12 pt-8 sm:px-8 lg:grid-cols-2 lg:gap-16 lg:px-12 lg:py-10">
        {/* Left introduction */}
        <section className="mx-auto w-full max-w-xl">
          <div
            className={`mb-7 inline-flex items-center gap-2 rounded-full border px-4 py-2 text-[10px] font-bold tracking-[0.18em] sm:text-xs ${
              isDark
                ? 'border-violet-500/30 bg-violet-500/10 text-violet-300'
                : 'border-violet-200 bg-violet-50 text-violet-700'
            }`}
          >
            <span className="h-2 w-2 rounded-full bg-emerald-400 shadow-[0_0_10px_#34d399]" />
            YOUR PERSONAL INTELLIGENCE HUB
          </div>

          <h2 className="text-4xl font-bold leading-[1.15] tracking-tight sm:text-5xl lg:text-6xl">
            Your life,
            <span className="mt-2 block bg-gradient-to-r from-violet-400 via-purple-400 to-indigo-400 bg-clip-text text-transparent">
              intelligently connected.
            </span>
          </h2>

          <p
            className={`mt-6 max-w-lg text-base leading-8 sm:text-lg ${
              isDark ? 'text-slate-400' : 'text-slate-600'
            }`}
          >
            Meet your personal AI assistant — a smarter way to
            manage information, remember what matters, and connect
            the tools you use every day.
          </p>

          {/* Features */}
          <div className="mt-10 grid grid-cols-1 gap-5 sm:grid-cols-2">
            {[
              {
                icon: '✧',
                title: 'Intelligent AI',
                description:
                  'Natural conversations and AI-powered assistance.',
              },
              {
                icon: '◈',
                title: 'Personal Memory',
                description:
                  'Remember preferences, facts and past decisions.',
              },
              {
                icon: '▦',
                title: 'Connected Workspace',
                description:
                  'Bring Gmail, Calendar and Drive together.',
              },
              {
                icon: '⌘',
                title: 'Agentic Workflows',
                description:
                  'Organize tasks and coordinate useful actions.',
              },
            ].map((feature) => (
              <div
                key={feature.title}
                className={`flex gap-4 rounded-2xl border p-4 transition hover:-translate-y-1 ${
                  isDark
                    ? 'border-slate-800 bg-slate-900/50 hover:border-violet-500/40'
                    : 'border-slate-200 bg-white/80 shadow-sm hover:border-violet-300'
                }`}
              >
                <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-violet-500/20 to-indigo-500/20 text-xl text-violet-400">
                  {feature.icon}
                </div>

                <div>
                  <h3 className="text-sm font-semibold">
                    {feature.title}
                  </h3>
                  <p
                    className={`mt-1 text-xs leading-5 ${
                      isDark ? 'text-slate-400' : 'text-slate-500'
                    }`}
                  >
                    {feature.description}
                  </p>
                </div>
              </div>
            ))}
          </div>

          <div
            className={`mt-8 flex items-center gap-3 text-sm ${
              isDark ? 'text-slate-400' : 'text-slate-600'
            }`}
          >
            <span className="flex h-6 w-6 items-center justify-center rounded-full bg-emerald-500/15 text-emerald-500">
              ✓
            </span>
            Your data stays under your control.
          </div>
        </section>

        {/* Login form */}
        <section className="mx-auto w-full max-w-md">
          <div
            className={`rounded-3xl border p-6 shadow-2xl backdrop-blur-xl sm:p-9 ${
              isDark
                ? 'border-slate-800 bg-slate-900/80 shadow-black/30'
                : 'border-slate-200 bg-white/90 shadow-slate-300/40'
            }`}
          >
            <div className="mb-8">
              <p className="text-xs font-bold tracking-[0.2em] text-violet-500">
                WELCOME BACK
              </p>

              <h2 className="mt-3 text-2xl font-bold tracking-tight sm:text-3xl">
                Sign in to your workspace
              </h2>

              <p
                className={`mt-3 text-sm leading-6 ${
                  isDark ? 'text-slate-400' : 'text-slate-500'
                }`}
              >
                Continue where you left off. Your personal assistant
                is ready.
              </p>
            </div>

            <form onSubmit={handleLogin} className="space-y-5">
              {/* Email */}
              <div>
                <label
                  htmlFor="email"
                  className={`mb-2 block text-sm font-medium ${
                    isDark ? 'text-slate-300' : 'text-slate-700'
                  }`}
                >
                  Email address
                </label>

                <input
                  id="email"
                  type="email"
                  placeholder="you@example.com"
                  autoComplete="email"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  required
                  className={`w-full rounded-xl border px-4 py-3.5 text-sm outline-none transition placeholder:text-slate-500 focus:border-violet-500 focus:ring-4 focus:ring-violet-500/10 ${
                    isDark
                      ? 'border-slate-700 bg-slate-950/70 text-white'
                      : 'border-slate-200 bg-slate-50 text-slate-900'
                  }`}
                />
              </div>

              {/* Password */}
              <div>
                <div className="mb-2 flex items-center justify-between gap-3">
                  <label
                    htmlFor="password"
                    className={`text-sm font-medium ${
                      isDark ? 'text-slate-300' : 'text-slate-700'
                    }`}
                  >
                    Password
                  </label>

                  <button
                    type="button"
                    onClick={handleForgotPassword}
                    disabled={loading}
                    className="text-xs font-semibold text-violet-500 transition hover:text-violet-400 disabled:opacity-50"
                  >
                    Forgot password?
                  </button>
                </div>

                <div className="relative">
                  <input
                    id="password"
                    type={showPassword ? 'text' : 'password'}
                    placeholder="Enter your password"
                    autoComplete="current-password"
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    required
                    className={`w-full rounded-xl border px-4 py-3.5 pr-16 text-sm outline-none transition placeholder:text-slate-500 focus:border-violet-500 focus:ring-4 focus:ring-violet-500/10 ${
                      isDark
                        ? 'border-slate-700 bg-slate-950/70 text-white'
                        : 'border-slate-200 bg-slate-50 text-slate-900'
                    }`}
                  />

                  <button
                    type="button"
                    onClick={() =>
                      setShowPassword((current) => !current)
                    }
                    className="absolute right-4 top-1/2 -translate-y-1/2 text-xs font-semibold text-violet-500 hover:text-violet-400"
                  >
                    {showPassword ? 'Hide' : 'Show'}
                  </button>
                </div>
              </div>

              {/* Submit */}
              <button
                type="submit"
                disabled={loading || googleLoading}
                className="flex w-full items-center justify-center gap-3 rounded-xl bg-gradient-to-r from-violet-600 to-indigo-600 px-5 py-3.5 text-sm font-bold text-white shadow-lg shadow-violet-600/20 transition hover:-translate-y-0.5 hover:from-violet-500 hover:to-indigo-500 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {loading ? 'Signing in...' : 'Sign in'}
                {!loading && <span className="text-lg">→</span>}
              </button>
            </form>

            {/* Feedback */}
            {message && (
              <div
                role="status"
                aria-live="polite"
                className={`mt-5 rounded-xl border px-4 py-3 text-sm leading-6 ${
                  message.type === 'success'
                    ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-500'
                    : 'border-red-500/30 bg-red-500/10 text-red-500'
                }`}
              >
                {message.text}
              </div>
            )}

            {/* Divider */}
            <div className="my-7 flex items-center gap-4">
              <div
                className={`h-px flex-1 ${
                  isDark ? 'bg-slate-800' : 'bg-slate-200'
                }`}
              />
              <span
                className={`text-[10px] font-bold tracking-[0.18em] ${
                  isDark ? 'text-slate-500' : 'text-slate-400'
                }`}
              >
                OR CONTINUE WITH
              </span>
              <div
                className={`h-px flex-1 ${
                  isDark ? 'bg-slate-800' : 'bg-slate-200'
                }`}
              />
            </div>

            {/* Google */}
            <button
              type="button"
              onClick={handleGoogleLogin}
              disabled={loading || googleLoading}
              className={`flex w-full items-center justify-center gap-3 rounded-xl border px-5 py-3.5 text-sm font-semibold transition disabled:cursor-not-allowed disabled:opacity-60 ${
                isDark
                  ? 'border-slate-700 bg-slate-950/50 text-slate-200 hover:border-slate-500 hover:bg-slate-800'
                  : 'border-slate-200 bg-white text-slate-700 hover:bg-slate-50'
              }`}
            >
              <svg
                width="19"
                height="19"
                viewBox="0 0 48 48"
                aria-hidden="true"
              >
                <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5Z" />
                <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.25 5.47-4.72 7.15l7.22 5.6c4.22-3.9 7.54-9.65 7.54-17.22Z" />
                <path fill="#FBBC05" d="M10.53 28.59A14.4 14.4 0 0 1 9.75 24c0-1.6.28-3.14.78-4.59l-7.98-6.19A23.8 23.8 0 0 0 0 24c0 3.9.93 7.59 2.56 10.78l7.97-6.19Z" />
                <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.9-5.78l-7.22-5.6c-2 1.34-4.55 2.13-8.68 2.13-6.26 0-11.57-4.22-13.46-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48Z" />
              </svg>

              <span>
                {googleLoading
                  ? 'Connecting...'
                  : 'Continue with Google'}
              </span>
            </button>

            <div
              className={`mt-6 flex items-center justify-center gap-3 text-xs ${
                isDark ? 'text-slate-400' : 'text-slate-500'
              }`}
            >
              <Link
                to="/privacy"
                className="font-medium text-violet-500 hover:text-violet-400"
              >
                Privacy Policy
              </Link>
              <span>•</span>
              <Link
                to="/terms"
                className="font-medium text-violet-500 hover:text-violet-400"
              >
                Terms of Service
              </Link>
            </div>

            <p
              className={`mt-8 text-center text-sm ${
                isDark ? 'text-slate-400' : 'text-slate-500'
              }`}
            >
              Don't have an account?{' '}
              <Link
                to="/signup"
                className="font-bold text-violet-500 hover:text-violet-400"
              >
                Create account
              </Link>
            </p>

            <p
              className={`mt-6 text-center text-[11px] leading-5 ${
                isDark ? 'text-slate-500' : 'text-slate-400'
              }`}
            >
              By continuing, you agree to use your personal workspace
              responsibly and keep your account credentials secure.
            </p>
          </div>

          <p
            className={`mt-6 text-center text-[10px] font-bold tracking-[0.2em] ${
              isDark ? 'text-slate-600' : 'text-slate-400'
            }`}
          >
            YOUR DATA. YOUR ASSISTANT. YOUR CONTROL.
          </p>
        </section>
      </main>
    </div>
  )
}

export default Login
