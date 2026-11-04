import { useEffect, useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { conversationService } from '../services/conversationService.js'

function normalizeRecipient(recipient) {
  if (typeof recipient === 'string') {
    const formatted = recipient.trim().match(/^(.*?)\s*<([^<>]+)>$/)
    if (formatted) {
      return {
        email: formatted[2].trim(),
        name: formatted[1].replace(/^["']|["']$/g, '').trim() || null,
      }
    }
    return recipient.trim()
      ? { email: recipient.trim(), name: null }
      : null
  }
  if (!recipient || typeof recipient !== 'object' || Array.isArray(recipient)) {
    return null
  }
  const email = recipient.email || recipient.address
  if (typeof email !== 'string' || !email.trim()) return null
  const name =
    recipient.name ||
    recipient.display_name ||
    recipient.displayName
  return {
    email: email.trim(),
    name: typeof name === 'string' && name.trim() ? name.trim() : null,
  }
}

function draftCopy(draft) {
  const value = draft && typeof draft === 'object' && !Array.isArray(draft)
    ? draft
    : {}
  const recipients = (field) =>
    Array.isArray(value[field])
      ? value[field].map(normalizeRecipient).filter(Boolean)
      : []
  return {
    draft_type: typeof value.draft_type === 'string' ? value.draft_type : 'new',
    to: recipients('to'),
    cc: recipients('cc'),
    bcc: recipients('bcc'),
    subject: typeof value.subject === 'string' ? value.subject : '',
    body: typeof value.body === 'string' ? value.body : '',
    source_message_id:
      typeof value.source_message_id === 'string'
        ? value.source_message_id
        : null,
    source_thread_id:
      typeof value.source_thread_id === 'string'
        ? value.source_thread_id
        : null,
  }
}

function malformedDraft(draft) {
  if (!draft || typeof draft !== 'object' || Array.isArray(draft)) return true
  if (typeof draft.body !== 'string' || typeof draft.subject !== 'string') {
    return true
  }
  return ['to', 'cc', 'bcc'].some(
    (field) =>
      draft[field] !== undefined &&
      (!Array.isArray(draft[field]) ||
        draft[field].some((recipient) => !normalizeRecipient(recipient))),
  )
}

function addressText(addresses) {
  return addresses
    .map((recipient) => {
      const normalized = normalizeRecipient(recipient)
      if (!normalized) return ''
      return typeof normalized === 'string'
        ? normalized
        : normalized.name
          ? `${normalized.name} <${normalized.email}>`
          : normalized.email
    })
    .filter(Boolean)
    .join(', ')
}

function addressesFromText(value) {
  return value
    .split(',')
    .map((address) => address.trim())
    .filter(Boolean)
    .map(normalizeRecipient)
    .filter(Boolean)
}

function canonicalDraft(draft) {
  return JSON.stringify({
    draft_type: draft.draft_type,
    to: draft.to || [],
    cc: draft.cc || [],
    bcc: draft.bcc || [],
    subject: draft.subject || '',
    body: draft.body || '',
    source_message_id: draft.source_message_id || null,
    source_thread_id: draft.source_thread_id || null,
  })
}

function EmailDraftCard({
  message,
  isDark,
  connectionId,
  onUpdated,
}) {
  const metadata =
    message?.metadata && typeof message.metadata === 'object'
      ? message.metadata
      : {}
  const rawDraft = metadata.email_draft
  const initialDraft = draftCopy(rawDraft)
  const [draft, setDraft] = useState(initialDraft)
  const [error, setError] = useState('')
  const [saveNotice, setSaveNotice] = useState('')
  const [saveBlockedLocally, setSaveBlockedLocally] = useState(false)
  const [metadataWarning, setMetadataWarning] = useState(
    malformedDraft(rawDraft)
      ? 'Some saved draft details were invalid and were safely omitted.'
      : '',
  )
  const requestInProgress = useRef(false)
  const gmailSaveInProgress = useRef(false)
  const [saveOperationId, setSaveOperationId] = useState(null)
  const autoRecoveryKey = useRef('')

  function userFacingError(mutationError, operation) {
    const detail =
      mutationError instanceof Error ? mutationError.message.toLowerCase() : ''
    if (/permission|scope|403/.test(detail)) {
      return 'Gmail permission is missing. Reconnect the account in Connectors and grant Gmail access.'
    }
    if (/connection|account|404/.test(detail)) {
      return 'The selected Gmail account or saved operation could not be found. Check Connectors and the conversation, then try again.'
    }
    if (/invalid|validation/.test(detail)) {
      return 'Some draft details are invalid. Review the recipients, subject, and message before saving.'
    }
    return operation === 'save'
      ? 'Gmail could not confirm whether the draft was created. Check the save status before attempting anything else.'
      : 'Unable to save these draft changes. Reload the conversation and try again.'
  }

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
      setSaveBlockedLocally(false)
      onUpdated(updatedMessage)
      if (emailDraft === null) return
      setDraft(draftCopy(updatedMessage.metadata.email_draft))
    },
    onError: (mutationError) => {
      requestInProgress.current = false
      setError(
        userFacingError(mutationError, 'update'),
      )
    },
  })

  const saveMutation = useMutation({
    mutationFn: ({ connection, operationId }) =>
      conversationService.saveEmailDraftToGmail(
        message.conversation_id,
        message.id,
        connection,
        operationId,
      ),
    onSuccess: (result) => {
      gmailSaveInProgress.current = false
      setSaveOperationId(null)
      setError('')
      setSaveNotice(
        result.persistence_warning
          ? 'Gmail confirmed the draft, but conversation history needs reconciliation.'
          : result.repeated
            ? 'This preview was already saved to Gmail Drafts.'
            : 'Draft saved to Gmail Drafts. It has not been sent.',
      )
      onUpdated({
        ...message,
        metadata: {
          ...message.metadata,
          gmail_draft_save: {
            state: 'saved',
            connection_id: result.connection_id,
            operation_id: result.operation_id,
            fingerprint: result.fingerprint,
            saved_operation_id: result.operation_id,
            saved_fingerprint: result.fingerprint,
            saved_connection_id: result.connection_id,
            saved_result: {
              draft_id: result.draft_id,
              message_id: result.message_id,
              thread_id: result.thread_id,
              connection_id: result.connection_id,
            },
            saved_preview: result.saved_preview,
            persistence_warning: result.persistence_warning,
          },
        },
      })
    },
    onError: (mutationError) => {
      gmailSaveInProgress.current = false
      setSaveNotice('')
      setSaveBlockedLocally(true)
      setError(
        userFacingError(mutationError, 'save'),
      )
    },
  })

  const saveStatusMutation = useMutation({
    mutationFn: ({ connection, operationId }) =>
      conversationService.getEmailDraftSaveStatus(
        message.conversation_id,
        message.id,
        connection,
        operationId,
      ),
    onSuccess: (result) => {
      const previous = message.metadata?.gmail_draft_save || {}
      const next = {
        ...previous,
        state: result.state,
        connection_id: result.connection_id,
        operation_id: result.operation_id,
      }
      if (result.state === 'saved') {
        delete next.persistence_warning
        next.saved_operation_id = result.operation_id
        next.saved_connection_id = result.connection_id
        next.saved_fingerprint = result.fingerprint || previous.fingerprint
        next.saved_result = {
          draft_id: result.draft_id,
          message_id: result.message_id,
          thread_id: result.thread_id,
          connection_id: result.connection_id,
        }
        if (result.saved_preview) next.saved_preview = result.saved_preview
        setSaveBlockedLocally(false)
        setSaveOperationId(null)
        setSaveNotice('Gmail confirmed this draft. No second draft was created.')
        setError('')
      } else if (result.state === 'saving') {
        setSaveBlockedLocally(true)
        setSaveNotice('The original Gmail draft operation is still processing.')
        setError('')
      } else {
        setSaveBlockedLocally(true)
        setSaveNotice('')
        setError(result.detail || 'The outcome remains uncertain. Do not retry.')
      }
      onUpdated({
        ...message,
        metadata: {
          ...message.metadata,
          gmail_draft_save: next,
        },
      })
    },
    onError: (statusError) => {
      const detail =
        statusError instanceof Error ? statusError.message.toLowerCase() : ''
      if (/operation was not found|operation not found/.test(detail)) {
        if (['saving', 'uncertain'].includes(saveRecord.state)) {
          setSaveBlockedLocally(true)
          setError(
            'The stored save operation could not be matched. Its outcome remains uncertain; reload the conversation before taking further action.',
          )
        } else {
          setSaveBlockedLocally(false)
          setError(
            'No save operation was recorded. It is safe to try saving again.',
          )
          setSaveOperationId(null)
        }
        return
      }
      setSaveBlockedLocally(true)
      setError(
        'Gmail could not inspect the save operation. Its outcome remains uncertain; do not retry.',
      )
    },
  })
  const checkSaveStatus = saveStatusMutation.mutate

  const saveRecord = metadata.gmail_draft_save || {}
  const savedDraft = draftCopy(metadata.email_draft)
  const recoveryOperationId =
    saveOperationId ||
    saveRecord.operation_id ||
    saveRecord.saved_operation_id
  const recoveryAccountMatches =
    !saveRecord.connection_id || saveRecord.connection_id === connectionId

  useEffect(() => {
    if (
      !connectionId ||
      !recoveryOperationId ||
      !recoveryAccountMatches ||
      !['saving', 'uncertain'].includes(saveRecord.state)
    ) {
      return
    }
    const key = `${connectionId}:${recoveryOperationId}`
    if (autoRecoveryKey.current === key) return
    autoRecoveryKey.current = key
    checkSaveStatus({
      connection: connectionId,
      operationId: recoveryOperationId,
    })
  }, [
    connectionId,
    recoveryOperationId,
    recoveryAccountMatches,
    checkSaveStatus,
    saveRecord.state,
  ])

  const isDirty = JSON.stringify(draft) !== JSON.stringify(savedDraft)
  const savedPreview = saveRecord.saved_preview
    ? draftCopy(saveRecord.saved_preview)
    : null
  const currentIsSaved = Boolean(
    saveRecord.state === 'saved' &&
    saveRecord.saved_connection_id === connectionId &&
    savedPreview &&
    canonicalDraft(savedPreview) === canonicalDraft(savedDraft),
  )
  const savedContentDiffers = Boolean(
    savedPreview &&
    canonicalDraft(savedPreview) !== canonicalDraft(savedDraft),
  )
  const savedForDifferentAccount = Boolean(
    saveRecord.saved_connection_id &&
    connectionId &&
    saveRecord.saved_connection_id !== connectionId,
  )
  const saveIsBlocked =
    saveBlockedLocally ||
    ['saving', 'uncertain'].includes(saveRecord.state)
  const disabled =
    updateMutation.isPending ||
    saveMutation.isPending ||
    saveStatusMutation.isPending
  const fieldClass = `w-full rounded-lg border px-3 py-2 text-sm outline-none transition focus:border-violet-500 disabled:opacity-60 ${
    isDark
      ? 'border-[#34384b] bg-[#171a27] text-slate-200 placeholder:text-slate-500'
      : 'border-slate-200 bg-white text-slate-800 placeholder:text-slate-400'
  }`

  function updateField(field, value) {
    setDraft((current) => ({ ...current, [field]: value }))
    setError('')
    setMetadataWarning('')
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

  function saveToGmail() {
    if (
      !connectionId ||
      isDirty ||
      currentIsSaved ||
      saveIsBlocked ||
      gmailSaveInProgress.current ||
      disabled
    ) {
      return
    }
    gmailSaveInProgress.current = true
    setError('')
    setSaveNotice('')
    const operationId = saveOperationId || crypto.randomUUID()
    setSaveOperationId(operationId)
    saveMutation.mutate({
      connection: connectionId,
      operationId,
    })
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
            {draftTypeLabel} · Not sent
            {currentIsSaved
              ? ' · Saved to Gmail Drafts'
              : savePreviewExists(saveRecord)
                ? ' · Saved Gmail draft is unchanged'
                : ' · Preview only'}
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
        {metadataWarning && (
          <p role="status" className="text-xs text-amber-500">
            {metadataWarning}
          </p>
        )}
        {saveNotice && (
          <p
            role="status"
            className={`text-xs ${
              saveRecord.persistence_warning
                ? isDark ? 'text-amber-300' : 'text-amber-700'
                : 'text-emerald-500'
            }`}
          >
            {saveNotice}
          </p>
        )}
        {saveRecord.state === 'saved' && saveRecord.saved_result?.draft_id && (
          <div className={`rounded-lg px-3 py-2 text-xs ${
            isDark
              ? 'bg-emerald-500/10 text-emerald-300'
              : 'bg-emerald-50 text-emerald-700'
          }`}>
            Saved Gmail Draft ID: <span className="font-mono">{saveRecord.saved_result.draft_id}</span>
            {saveRecord.saved_connection_id !== connectionId && (
              <span className="ml-1">· saved in a different selected account</span>
            )}
          </div>
        )}
        {(saveIsBlocked || saveRecord.persistence_warning) &&
          recoveryOperationId &&
          connectionId &&
          recoveryAccountMatches && (
          <button
            type="button"
            onClick={() =>
              saveStatusMutation.mutate({
                connection: connectionId,
                operationId: recoveryOperationId,
              })
            }
            disabled={disabled}
            className={`rounded-lg border px-3 py-2 text-xs font-semibold disabled:cursor-not-allowed disabled:opacity-50 ${
              isDark
                ? 'border-amber-500/30 text-amber-300'
                : 'border-amber-200 text-amber-700'
            }`}
          >
            {saveStatusMutation.isPending
              ? 'Checking Gmail Drafts...'
              : 'Check save status'}
          </button>
        )}
        {saveIsBlocked &&
          saveRecord.connection_id &&
          connectionId &&
          !recoveryAccountMatches && (
          <p className={`text-xs ${isDark ? 'text-amber-300' : 'text-amber-700'}`}>
            Select the Gmail account used for this save to inspect its outcome.
          </p>
        )}
        {savedContentDiffers && !isDirty && (
          <p className={`text-xs ${isDark ? 'text-amber-300' : 'text-amber-700'}`}>
            This preview has changed since the previous save. Saving again creates a new Gmail draft; it does not update or replace the existing one.
          </p>
        )}
        {savedForDifferentAccount && (
          <p className={`text-xs ${isDark ? 'text-amber-300' : 'text-amber-700'}`}>
            The saved Gmail draft is in a different account. Saving this preview will create a separate draft in the selected account.
          </p>
        )}
        {!connectionId && (
          <p className={`text-xs ${isDark ? 'text-slate-400' : 'text-slate-500'}`}>
            To save this preview to Gmail, connect an account or set a default
            account in{' '}
            <Link
              to="/dashboard/connectors"
              className="font-semibold text-violet-400 hover:text-violet-300"
            >
              Connectors
            </Link>
            .
          </p>
        )}
        {isDirty && connectionId && (
          <p className={`text-xs ${isDark ? 'text-amber-300' : 'text-amber-700'}`}>
            Save your preview changes first. Gmail saves use the persisted version shown in this conversation.
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
          <div className="flex flex-col gap-2 sm:flex-row">
            <button
              type="submit"
              disabled={disabled || !isDirty}
              className={`rounded-lg border px-4 py-2 text-xs font-semibold transition disabled:cursor-not-allowed disabled:opacity-50 ${
                isDark
                  ? 'border-[#34384b] text-slate-300 hover:bg-white/5'
                  : 'border-slate-200 text-slate-700 hover:bg-slate-50'
              }`}
            >
              {updateMutation.isPending && updateMutation.variables !== null
                ? 'Saving changes...'
                : 'Save preview changes'}
            </button>
            {connectionId && (
              <button
                type="button"
                onClick={saveToGmail}
                disabled={
                  disabled ||
                  isDirty ||
                  currentIsSaved ||
                  saveIsBlocked
                }
                className="rounded-lg bg-violet-600 px-4 py-2 text-xs font-semibold text-white transition hover:bg-violet-500 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {saveMutation.isPending
                  ? 'Saving to Gmail...'
                  : saveIsBlocked
                    ? 'Save outcome needs review'
                    : currentIsSaved
                      ? 'Saved to Gmail Drafts'
                      : 'Save to Gmail Drafts'}
              </button>
            )}
          </div>
        </div>
      </div>
    </form>
  )
}

function savePreviewExists(saveRecord) {
  return Boolean(
    saveRecord.saved_preview ||
    saveRecord.saved_result?.draft_id,
  )
}

export default EmailDraftCard
