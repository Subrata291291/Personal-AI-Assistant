import { useState } from 'react'
import { Link } from 'react-router-dom'

function PrivacyPolicy() {
  const [theme, setTheme] = useState('dark')
  const isDark = theme === 'dark'

  return (
    <div
      className={`min-h-screen overflow-x-hidden transition-colors duration-300 ${
        isDark ? 'bg-[#080b14] text-white' : 'bg-slate-50 text-slate-900'
      }`}
    >
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
              Privacy policy
            </p>
          </div>
        </Link>

        <button
          type="button"
          onClick={() => setTheme((current) => (current === 'dark' ? 'light' : 'dark'))}
          className={`rounded-full border px-4 py-2 text-sm font-medium transition hover:-translate-y-0.5 ${
            isDark
              ? 'border-slate-700 bg-slate-900/80 text-slate-200 hover:bg-slate-800'
              : 'border-slate-200 bg-white text-slate-700 shadow-sm hover:bg-slate-100'
          }`}
        >
          {isDark ? '☀ Light mode' : '☾ Dark mode'}
        </button>
      </header>

      <main className="relative z-10 mx-auto max-w-4xl px-5 pb-16 pt-8 sm:px-8 lg:px-12">
        <div
          className={`rounded-3xl border p-6 shadow-2xl backdrop-blur-xl sm:p-9 ${
            isDark
              ? 'border-slate-800 bg-slate-900/80 shadow-black/30'
              : 'border-slate-200 bg-white/90 shadow-slate-300/40'
          }`}
        >
          <p className="text-xs font-bold tracking-[0.2em] text-violet-500">
            PRIVACY POLICY
          </p>
          <h2 className="mt-4 text-3xl font-bold tracking-tight sm:text-4xl">
            How Personal AI handles your data
          </h2>
          <p
            className={`mt-5 text-sm leading-7 ${
              isDark ? 'text-slate-300' : 'text-slate-600'
            }`}
          >
            This Privacy Policy describes the personal data currently handled by the Personal AI Assistant application in this codebase. It is written to match the implemented backend and frontend features, not a broader or hypothetical product promise.
          </p>

          <div className="mt-10 space-y-8 text-sm leading-7">
            <section>
              <h3 className="text-xl font-semibold">1. Information the app may store</h3>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                The app stores account and usage data required to operate the assistant. In the current implementation, this includes:
              </p>
              <ul className={`mt-3 list-disc space-y-2 pl-5 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                <li>Supabase Auth account details such as email address and authentication state.</li>
                <li>User profile metadata collected during sign-up or account updates.</li>
                <li>Conversation history, user messages, assistant responses, and message metadata.</li>
                <li>Persistent memory records, including extracted user preferences and facts, stored in PostgreSQL with vector embeddings.</li>
                <li>User settings such as whether automatic memory storage is enabled.</li>
                <li>Google connection metadata and OAuth credentials tied to a specific user.</li>
              </ul>
            </section>

            <section>
              <h3 className="text-xl font-semibold">2. Authentication and account data</h3>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                The app uses Supabase Auth for sign-in, sign-up, email verification, and session management. The frontend authenticates by sending a Supabase access token to the backend. The backend then verifies the token and scopes requests to the authenticated user.
              </p>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                The current codebase also supports Google OAuth for account connection. Google tokens are exchanged for access and refresh tokens and are stored alongside the user’s Google connection record. The backend encrypts OAuth access and refresh token material before storing it using AES-256-GCM encryption in the application credential layer.
              </p>
            </section>

            <section>
              <h3 className="text-xl font-semibold">3. AI processing and provider use</h3>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                User conversations, memory content, and certain Gmail-related request data may be sent to configured AI providers for response generation, intent detection, memory extraction, and retrieval. In the current implementation, the backend is configured to use Groq, OpenRouter, Google Gemini, and OpenAI depending on provider availability and fallback order.
              </p>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                The app does not store API keys in the client. Provider keys are configured server-side in the backend environment. The exact model and provider used for a response are recorded in the app metadata for that request.
              </p>
            </section>

            <section>
              <h3 className="text-xl font-semibold">4. Google integrations</h3>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                The current codebase includes a working Gmail integration. The app may authenticate a user to Google, store a Google connection, and call Gmail APIs to read, search, send, reply, archive, restore, trash, and draft email content for the connected user.
              </p>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                Calendar, Contacts, and Drive are not implemented as working assistant integrations in the current backend and workflow graph. The current assistant explicitly routes those requests to an unavailable/integration-not-implemented path, so the application should not claim that those services are live features in this build.
              </p>
            </section>

            <section>
              <h3 className="text-xl font-semibold">5. Memory and conversation records</h3>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                The app can automatically extract memories from user messages, classify them, and store them as user-scoped records. The memory pipeline includes validation, deduplication, similarity checks, and manual review for conflicting entries. Some memory entries may include metadata, source conversation IDs, and importance/confidence values.
              </p>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                Memory content is used to personalize later assistant responses. This retrieval is user-scoped and depends on semantic similarity to the current input prompt.
              </p>
            </section>

            <section>
              <h3 className="text-xl font-semibold">6. Data sharing</h3>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                The application may share selected data with the AI providers it calls and with Google APIs when a user is using Gmail-connected features. The backend also uses the authenticated user’s identity to scope database access.
              </p>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                This application does not implement a separate data brokerage or resale model, and no explicit “never share” guarantee is made beyond the concrete technical controls implemented in the current codebase.
              </p>
            </section>

            <section>
              <h3 className="text-xl font-semibold">7. Data deletion and retention</h3>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                The current implementation includes user-scoped delete actions for conversations, memories, and Google connection records. Users can delete individual conversation records, delete stored memory entries, and disconnect Google accounts from the app.
              </p>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                There is no implemented automated “delete my account” workflow in this codebase. A user account and all associated app records are not automatically removed by a single account-deletion feature in the current build.
              </p>
            </section>

            <section>
              <h3 className="text-xl font-semibold">8. Security</h3>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                The application uses Supabase Auth for user accounts, PostgreSQL with user-scoped records, and enforces access by authenticated user identity. OAuth credentials are encrypted before persistence, and Google token refresh logic is built into the backend.
              </p>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                The app is designed to be user-scoped and to limit access to a user’s own records, but reasonable security practices still require strong passwords, careful handling of third-party credentials, and correct deployment configuration.
              </p>
            </section>

            <section>
              <h3 className="text-xl font-semibold">9. Changes to this policy</h3>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                This policy reflects the current implementation in the repository. If the product grows to include additional providers, features, or data-handling changes, the policy should be updated to reflect those changes before they are released.
              </p>
            </section>
          </div>

          <div className={`mt-10 flex flex-wrap gap-3 text-sm ${isDark ? 'text-slate-400' : 'text-slate-500'}`}>
            <Link to="/terms" className="font-medium text-violet-500 hover:text-violet-400">
              Terms of Service
            </Link>
            <Link to="/login" className="font-medium text-violet-500 hover:text-violet-400">
              Back to login
            </Link>
          </div>
        </div>
      </main>
    </div>
  )
}

export default PrivacyPolicy
