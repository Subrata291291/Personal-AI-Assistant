import { useState } from 'react'
import {
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { useOutletContext } from 'react-router-dom'

import AppIcon from '../components/AppIcon'
import { useAuth } from '../context/AuthContext'
import { api } from '../lib/api.js'

function Settings() {
  const { user, updateProfile, updatePassword } = useAuth()
  const { isDark, setTheme, surface, muted, hoverSurface } =
    useOutletContext()
  const queryClient = useQueryClient()
  const userId = user?.id
  const settingsKey = ['user-settings', userId]
  const metadata = user?.user_metadata || {}
  const currentName =
    metadata.full_name ||
    metadata.name ||
    user?.email?.split('@')[0] ||
    ''

  const [fullName, setFullName] = useState(currentName)
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [notice, setNotice] = useState(null)

  const settingsQuery = useQuery({
    queryKey: settingsKey,
    queryFn: () => api.get('/api/v1/me/settings'),
    enabled: Boolean(userId),
  })

  const profileMutation = useMutation({
    mutationFn: (name) => updateProfile({ fullName: name }),
    onSuccess: (_updatedUser, name) => {
      setFullName(name)
      setNotice({ type: 'success', text: 'Profile updated.' })
    },
    onError: (error) => {
      setNotice({
        type: 'error',
        text: error.message || 'Unable to update your profile.',
      })
    },
  })

  const passwordMutation = useMutation({
    mutationFn: (password) => updatePassword(password),
    onSuccess: () => {
      setNewPassword('')
      setConfirmPassword('')
      setNotice({ type: 'success', text: 'Password changed successfully.' })
    },
    onError: (error) => {
      setNotice({
        type: 'error',
        text: error.message || 'Unable to change your password.',
      })
    },
  })

  const memoryMutation = useMutation({
    mutationFn: (memoryEnabled) =>
      api.patch('/api/v1/me/settings', { memory_enabled: memoryEnabled }),
    onMutate: async (memoryEnabled) => {
      await queryClient.cancelQueries({ queryKey: settingsKey })
      const previousSettings = queryClient.getQueryData(settingsKey)
      queryClient.setQueryData(settingsKey, (current) => ({
        ...(current || {}),
        memory_enabled: memoryEnabled,
      }))
      return { previousSettings }
    },
    onSuccess: (updatedSettings) => {
      queryClient.setQueryData(settingsKey, updatedSettings)
      setNotice({ type: 'success', text: 'Memory preference updated.' })
    },
    onError: (error, _memoryEnabled, context) => {
      if (context?.previousSettings) {
        queryClient.setQueryData(settingsKey, context.previousSettings)
      }
      setNotice({
        type: 'error',
        text: error.message || 'Unable to update memory settings.',
      })
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: settingsKey })
    },
  })

  const inputStyle = isDark
    ? 'border-[#303448] bg-[#191c29] text-slate-100 placeholder:text-slate-500'
    : 'border-slate-200 bg-slate-50 text-slate-800 placeholder:text-slate-400'

  const memoryEnabled = settingsQuery.data?.memory_enabled ?? true
  const nameChanged = fullName.trim() !== currentName

  return (
    <div className="mx-auto max-w-4xl space-y-6 sm:space-y-8">
      <header>
        <p className="text-xs font-bold tracking-[1.5px] text-violet-400">
          PERSONALIZE YOUR WORKSPACE
        </p>
        <h1 className="mt-2 text-2xl font-bold tracking-tight sm:text-3xl">
          Settings
        </h1>
        <p className={`mt-2 text-sm leading-6 ${muted}`}>
          Manage your profile, memory preferences, and appearance.
        </p>
      </header>

      {notice && (
        <div
          role="status"
          className={`rounded-xl border px-4 py-3 text-sm ${
            notice.type === 'success'
              ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-500'
              : 'border-rose-500/30 bg-rose-500/10 text-rose-500'
          }`}
        >
          {notice.text}
        </div>
      )}

      <section className={`rounded-2xl border p-5 sm:p-7 ${surface}`}>
        <div className="mb-6">
          <h2 className="text-lg font-bold">Profile</h2>
          <p className={`mt-1 text-sm ${muted}`}>
            Update the name displayed across your workspace.
          </p>
        </div>

        <form
          className="space-y-5"
          onSubmit={(event) => {
            event.preventDefault()
            const name = fullName.trim()
            if (name && name !== currentName) {
              setNotice(null)
              profileMutation.mutate(name)
            }
          }}
        >
          <label className="block">
            <span className="mb-2 block text-sm font-medium">Full name</span>
            <input
              value={fullName}
              onChange={(event) => setFullName(event.target.value)}
              maxLength={100}
              required
              className={`w-full rounded-xl border px-4 py-3 text-sm outline-none transition focus:border-violet-500 ${inputStyle}`}
            />
          </label>

          <label className="block">
            <span className="mb-2 block text-sm font-medium">Email</span>
            <input
              value={user?.email || ''}
              readOnly
              className={`w-full cursor-not-allowed rounded-xl border px-4 py-3 text-sm opacity-75 ${inputStyle}`}
            />
            <span className={`mt-2 block text-xs ${muted}`}>
              Email address is managed by your sign-in provider.
            </span>
          </label>

          <button
            type="submit"
            disabled={
              !fullName.trim() ||
              !nameChanged ||
              profileMutation.isPending
            }
            className="rounded-xl bg-gradient-to-r from-violet-600 to-indigo-600 px-5 py-3 text-sm font-semibold text-white transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {profileMutation.isPending ? 'Saving...' : 'Save profile'}
          </button>
        </form>
      </section>

      <section className={`rounded-2xl border p-5 sm:p-7 ${surface}`}>
        <div className="mb-6">
          <h2 className="text-lg font-bold">Change password</h2>
          <p className={`mt-1 text-sm leading-6 ${muted}`}>
            Set a new password for signing in to your account.
          </p>
        </div>
        <form
          className="space-y-5"
          onSubmit={(event) => {
            event.preventDefault()
            if (
              newPassword.length >= 8 &&
              newPassword === confirmPassword
            ) {
              setNotice(null)
              passwordMutation.mutate(newPassword)
            }
          }}
        >
          <label className="block">
            <span className="mb-2 block text-sm font-medium">
              New password
            </span>
            <input
              type="password"
              autoComplete="new-password"
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
              minLength={8}
              required
              className={`w-full rounded-xl border px-4 py-3 text-sm outline-none transition focus:border-violet-500 ${inputStyle}`}
            />
            <span className={`mt-2 block text-xs ${muted}`}>
              Use at least 8 characters.
            </span>
          </label>
          <label className="block">
            <span className="mb-2 block text-sm font-medium">
              Confirm new password
            </span>
            <input
              type="password"
              autoComplete="new-password"
              value={confirmPassword}
              onChange={(event) => setConfirmPassword(event.target.value)}
              minLength={8}
              required
              className={`w-full rounded-xl border px-4 py-3 text-sm outline-none transition focus:border-violet-500 ${inputStyle}`}
            />
          </label>
          {confirmPassword && newPassword !== confirmPassword && (
            <p className="text-sm text-rose-500">
              The passwords do not match.
            </p>
          )}
          <button
            type="submit"
            disabled={
              newPassword.length < 8 ||
              newPassword !== confirmPassword ||
              passwordMutation.isPending
            }
            className="rounded-xl bg-gradient-to-r from-violet-600 to-indigo-600 px-5 py-3 text-sm font-semibold text-white transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {passwordMutation.isPending ? 'Changing password...' : 'Change password'}
          </button>
        </form>
      </section>

      <section className={`rounded-2xl border p-5 sm:p-7 ${surface}`}>
        <div className="mb-5">
          <h2 className="text-lg font-bold">Memory preferences</h2>
          <p className={`mt-1 text-sm leading-6 ${muted}`}>
            Control whether the assistant can save useful details to long-term
            memory.
          </p>
        </div>

        {settingsQuery.isLoading ? (
          <p className={`text-sm ${muted}`}>Loading preferences...</p>
        ) : settingsQuery.isError ? (
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-sm text-rose-500">
              {settingsQuery.error.message || 'Unable to load memory preferences.'}
            </p>
            <button
              type="button"
              onClick={() => settingsQuery.refetch()}
              className={`rounded-lg px-3 py-2 text-sm font-semibold ${hoverSurface}`}
            >
              Try again
            </button>
          </div>
        ) : (
          <label className="flex cursor-pointer items-center justify-between gap-4">
            <span>
              <span className="block text-sm font-semibold">
                Enable long-term memory
              </span>
              <span className={`mt-1 block text-xs leading-5 ${muted}`}>
                {memoryEnabled
                  ? 'Personal details may be saved to help future conversations.'
                  : 'New details will not be added to long-term memory.'}
              </span>
            </span>
            <input
              type="checkbox"
              checked={memoryEnabled}
              disabled={memoryMutation.isPending}
              onChange={(event) => {
                setNotice(null)
                memoryMutation.mutate(event.target.checked)
              }}
              className="h-5 w-5 shrink-0 accent-violet-500"
            />
          </label>
        )}
      </section>

      <section className={`rounded-2xl border p-5 sm:p-7 ${surface}`}>
        <div className="mb-5">
          <h2 className="text-lg font-bold">Appearance</h2>
          <p className={`mt-1 text-sm ${muted}`}>
            Choose the look of your workspace.
          </p>
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {['dark', 'light'].map((option) => (
            <button
              key={option}
              type="button"
              onClick={() => setTheme(option)}
              aria-pressed={(isDark ? 'dark' : 'light') === option}
              className={`flex items-center justify-between rounded-xl border p-4 text-left capitalize transition ${
                (isDark ? 'dark' : 'light') === option
                  ? 'border-violet-500 bg-violet-500/10'
                  : `border-inherit ${hoverSurface}`
              }`}
            >
              <span className="font-semibold">{option} theme</span>
              <span className={option === 'dark' ? 'text-indigo-300' : 'text-amber-500'}>
                <AppIcon
                  name={option === 'dark' ? 'moon' : 'sun'}
                  className="h-5 w-5"
                />
              </span>
            </button>
          ))}
        </div>
      </section>
    </div>
  )
}

export default Settings
