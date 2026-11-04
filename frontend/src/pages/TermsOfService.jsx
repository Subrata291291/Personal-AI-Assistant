import { useState } from 'react'
import { Link } from 'react-router-dom'

function TermsOfService() {
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
              Terms of service
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
            TERMS OF SERVICE
          </p>
          <h2 className="mt-4 text-3xl font-bold tracking-tight sm:text-4xl">
            Use of the Personal AI Assistant
          </h2>
          <p
            className={`mt-5 text-sm leading-7 ${
              isDark ? 'text-slate-300' : 'text-slate-600'
            }`}
          >
            These Terms of Service apply to the current Personal AI Assistant product as implemented in this repository. They are intended to describe the actual service behavior available in this codebase and do not assume additional features or guarantees beyond what is implemented.
          </p>

          <div className="mt-10 space-y-8 text-sm leading-7">
            <section>
              <h3 className="text-xl font-semibold">1. Service description</h3>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                This service provides a personal AI workspace for conversations, personal memory, and connected work tools. The current implementation includes account authentication, conversation history, memory extraction and retrieval, and an optional Gmail integration.
              </p>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                The platform is designed to help users organize information and receive AI assistance. It is not a guaranteed, always-available system, and AI responses, third-party API results, and network access may vary by provider availability and service status.
              </p>
            </section>

            <section>
              <h3 className="text-xl font-semibold">2. Your content and responsibilities</h3>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                You retain ownership of the content you provide to the service, such as messages, memories, emails, and account metadata. By using the service, you agree that the application may process that content as needed to provide conversations, memory features, and connected integrations.
              </p>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                You agree not to use the service to store or transmit sensitive credentials, secrets, or private data in a way that you do not intend to process through the app’s configured systems. The memory extractor includes policy checks against obvious sensitive patterns, but it is not a complete data-loss prevention system.
              </p>
            </section>

            <section>
              <h3 className="text-xl font-semibold">3. Account access and credentials</h3>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                You are responsible for keeping your login credentials, email verification, and connected Google account access secure. This app uses Supabase Auth for user accounts and Google OAuth for external account connections.
              </p>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                When you connect Google services, the associated access and refresh token information is handled by the backend and stored with user-scoped records. The current codebase stores encrypted token data for OAuth-related use in the app’s credential flow.
              </p>
            </section>

            <section>
              <h3 className="text-xl font-semibold">4. AI provider usage</h3>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                The app may send selected request data to third-party AI providers such as Groq, OpenRouter, Gemini, or OpenAI. These calls are made to generate responses, classify intent, or extract memories. The AI provider’s own terms and privacy policies apply to the data transmitted to that provider.
              </p>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                The service may switch providers automatically based on configured priority and failure handling. This is part of the current implementation but does not guarantee uninterrupted service availability.
              </p>
            </section>

            <section>
              <h3 className="text-xl font-semibold">5. Google integration status</h3>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                The current product includes a real Gmail integration. This means a user may connect a Google account and use Gmail features such as searching, reading, drafting, replying, forwarding, and sending email through the app’s Gmail service layer.
              </p>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                Calendar, Contacts, and Drive are not implemented as live assistant actions in the current build, and the assistant workflow explicitly marks those features as unavailable. If a feature is not implemented in this repository, it should not be treated as available functionality.
              </p>
            </section>

            <section>
              <h3 className="text-xl font-semibold">6. Memory features</h3>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                The app may create and store user memories to improve future assistant responses. These memories are stored in a user-bound data model, can be reviewed, and can be deleted by the user through the app’s memory management endpoints.
              </p>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                Memory storage is not a guarantee that any specific information will always be remembered, retrieved, or used in the future. Retrieval is based on similarity, relevance, and the current prompt.
              </p>
            </section>

            <section>
              <h3 className="text-xl font-semibold">7. Deletion and account lifecycle</h3>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                The current implementation permits deletion of individual conversations, memory records, and Google connection records. Users can also disconnect connected Google accounts from the app.
              </p>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                This codebase does not currently include a single, full user-account deletion workflow that removes every related record from the app and from the integrated providers in one action. You should assume that user data may remain until it is explicitly deleted through the implemented workflows.
              </p>
            </section>

            <section>
              <h3 className="text-xl font-semibold">8. Availability and disclaimers</h3>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                The service is provided “as is.” The team does not guarantee uninterrupted operation, perfect AI accuracy, or complete preservation of data outside the implemented workflows and infrastructure. Third-party services, Google APIs, LLM providers, and Supabase may experience outages, rate limiting, or policy restrictions beyond the app’s control.
              </p>
            </section>

            <section>
              <h3 className="text-xl font-semibold">9. Changes to the service</h3>
              <p className={`mt-3 ${isDark ? 'text-slate-300' : 'text-slate-600'}`}>
                The application may evolve over time. New features, new integrations, and new data-processing behavior should be documented to keep the product terms and privacy statements aligned with the actual backend implementation.
              </p>
            </section>
          </div>

          <div className={`mt-10 flex flex-wrap gap-3 text-sm ${isDark ? 'text-slate-400' : 'text-slate-500'}`}>
            <Link to="/privacy" className="font-medium text-violet-500 hover:text-violet-400">
              Privacy Policy
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

export default TermsOfService
