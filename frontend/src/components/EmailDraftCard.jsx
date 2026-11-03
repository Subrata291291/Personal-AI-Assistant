import { useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'

import { conversationService } from '../services/conversationService.js'

function draftCopy(draft) {
  return {
    ...draft,
    to: Array.isArray(draft.to) ? [...draft.to] : [],
    cc: Array.isArray(draft.cc) ? [...draft.cc] : [],
    bcc: Array.isArray(draft.bcc) ? [...draft.bcc] : [],
  }
}

function addressText(addresses) {
  return addresses.join(', ')
}

function addressesFromText(value) {
  return value
    .split(',')
    .map((address) => address.trim())
    .filter(Boolean)
}

function EmailDraftCard({
  message,
  isDark,
  onUpdated,
}) {
  const initialDraft = draftCopy(message.metadata.email_draft)
  const [draft, setDraft] = useState(initialDraft)
  const [error, setError] = useState('')
  const requestInProgress = useRef(false)

  const updateMutation = useMutation({
    mutationFn: (emailDraft) =>
      conversationService.updateEmailDraft(
        message.conversation_id,
        message.id,
        emailDraft,
      ),
    onSuccess: (updatedMessage, emailDraft) => {
      requestInProgress.current = false
      setError('')
      onUpdated(updatedMessage)
      if (emailDraft === null) return
      setDraft(draftCopy(updatedMessage.metadata.email_draft))
    },
    onError: (mutationError) => {
      requestInProgress.current = false
      setError(
        mutationError?.message ||
        'Unable to save this draft preview.',
      )
    },
  })

  const savedDraft = draftCopy(message.metadata.email_draft)
  const isDirty = JSON.stringify(draft) !== JSON.stringify(savedDraft)
  const disabled = updateMutation.isPending
  const fieldClass = `w-full rounded-lg border px-3 py-2 text-sm outline-none transition focus:border-violet-500 disabled:opacity-60 ${
    isDark
      ? 'border-[#34384b] bg-[#171a27] text-slate-200 placeholder:text-slate-500'
      : 'border-slate-200 bg-white text-slate-800 placeholder:text-slate-400'
  }`

  function updateField(field, value) {
    setDraft((current) => ({ ...current, [field]: value }))
    setError('')
  }

  function saveDraft(event) {
    event.preventDefault()
    if (requestInProgress.current || disabled || !isDirty) return
    requestInProgress.current = true
    updateMutation.mutate(draft)
  }

  function discardDraft() {
    if (requestInProgress.current || disabled) return
    requestInProgress.current = true
    setError('')
    updateMutation.mutate(null)
  }

  const draftTypeLabel = {
    new: 'New email',
    reply: 'Reply',
    reply_all: 'Reply all',
    forward: 'Forward',
  }[draft.draft_type] || 'Email'

  return (
    <form
      onSubmit={saveDraft}
      className={`mt-4 overflow-hidden rounded-xl border text-left shadow-sm ${
        isDark
          ? 'border-[#393349] bg-[#191b29]'
          : 'border-violet-200 bg-white'
      }`}
    >
      <div className={`flex items-center justify-between gap-3 border-b px-4 py-3 ${
        isDark
          ? 'border-[#303245] bg-violet-500/5'
          : 'border-violet-100 bg-violet-50/70'
      }`}>
        <div className="min-w-0">
          <div className="text-sm font-semibold">Email draft preview</div>
          <div className={`mt-0.5 text-xs ${isDark ? 'text-slate-400' : 'text-slate-500'}`}>
            {draftTypeLabel} · Not sent or saved to Gmail
          </div>
        </div>
        <span className="shrink-0 rounded-full bg-violet-500/10 px-2.5 py-1 text-[11px] font-semibold text-violet-400">
          Editable
        </span>
      </div>

      <div className="space-y-3 p-4">
        {[
          ['to', 'To'],
          ['cc', 'Cc'],
          ['bcc', 'Bcc'],
        ].map(([field, label]) => (
          <label key={field} className="grid gap-1.5 sm:grid-cols-[52px_1fr] sm:items-center">
            <span className={`text-xs font-semibold ${isDark ? 'text-slate-400' : 'text-slate-500'}`}>
              {label}
            </span>
            <input
              type="text"
              value={addressText(draft[field])}
              onChange={(event) =>
                updateField(field, addressesFromText(event.target.value))
              }
              disabled={disabled}
              placeholder="Add email addresses, separated by commas"
              autoComplete="off"
              className={fieldClass}
            />
          </label>
        ))}

        <label className="grid gap-1.5 sm:grid-cols-[52px_1fr] sm:items-center">
          <span className={`text-xs font-semibold ${isDark ? 'text-slate-400' : 'text-slate-500'}`}>
            Subject
          </span>
          <input
            type="text"
            value={draft.subject}
            onChange={(event) => updateField('subject', event.target.value)}
            disabled={disabled}
            maxLength={500}
            className={fieldClass}
          />
        </label>

        <label className="block">
          <span className={`mb-1.5 block text-xs font-semibold ${isDark ? 'text-slate-400' : 'text-slate-500'}`}>
            Message
          </span>
          <textarea
            value={draft.body}
            onChange={(event) => updateField('body', event.target.value)}
            disabled={disabled}
            maxLength={20000}
            rows={8}
            className={`${fieldClass} resize-y leading-6`}
          />
        </label>

        {error && (
          <p role="alert" className="text-xs text-rose-400">
            {error}
          </p>
        )}

        <div className={`flex flex-col-reverse gap-2 border-t pt-3 sm:flex-row sm:items-center sm:justify-between ${
          isDark ? 'border-[#303245]' : 'border-slate-100'
        }`}>
          <button
            type="button"
            onClick={discardDraft}
            disabled={disabled}
            className={`rounded-lg px-3 py-2 text-xs font-semibold transition disabled:cursor-not-allowed disabled:opacity-50 ${
              isDark
                ? 'text-slate-400 hover:bg-white/5 hover:text-rose-300'
                : 'text-slate-500 hover:bg-rose-50 hover:text-rose-600'
            }`}
          >
            {updateMutation.isPending && updateMutation.variables === null
              ? 'Discarding...'
              : 'Discard draft'}
          </button>
          <button
            type="submit"
            disabled={disabled || !isDirty}
            className="rounded-lg bg-violet-600 px-4 py-2 text-xs font-semibold text-white transition hover:bg-violet-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {updateMutation.isPending && updateMutation.variables !== null
              ? 'Saving...'
              : 'Save changes'}
          </button>
        </div>
      </div>
    </form>
  )
}

export default EmailDraftCard
