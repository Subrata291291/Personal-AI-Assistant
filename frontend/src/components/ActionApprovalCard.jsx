import { useEffect, useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'

import { actionApprovalService } from '../services/actionApprovalService.js'
import { formatRequestError } from '../lib/errorMessage.js'

function displayValue(value) {
  if (Array.isArray(value)) return value.join(', ')
  if (value && typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function updateProposalMessage(message, proposal, onUpdated) {
  onUpdated({
    ...message,
    metadata: {
      ...message.metadata,
      action_approval: proposal,
    },
  })
}

function ActionApprovalCard({ message, isDark, onUpdated }) {
  const proposal = message.metadata.action_approval
  const [error, setError] = useState('')
  const [expired, setExpired] = useState(false)
  const requestInProgress = useRef(false)
  useEffect(() => {
    if (proposal.status !== 'pending') return undefined

    const expiresIn = new Date(proposal.expires_at).getTime() - Date.now()
    const timeoutId = window.setTimeout(
      () => setExpired(true),
      Math.max(0, expiresIn),
    )
    return () => window.clearTimeout(timeoutId)
  }, [proposal.expires_at, proposal.status])

  const mutation = useMutation({
    mutationFn: (decision) =>
      decision === 'approve'
        ? actionApprovalService.approve(proposal.proposal_id)
        : actionApprovalService.reject(proposal.proposal_id),
    onSuccess: (response) => {
      setError('')
      updateProposalMessage(message, response.proposal, onUpdated)
    },
    onError: (mutationError) => {
      setError(formatRequestError(
        mutationError,
        'Unable to update this approval proposal.',
      ))
    },
    onSettled: () => {
      requestInProgress.current = false
    },
  })

  const executeMutation = useMutation({
    mutationFn: () =>
      actionApprovalService.execute(proposal.proposal_id),
    onSuccess: (response) => {
      setError('')
      updateProposalMessage(message, response.proposal, onUpdated)
    },
    onError: async (mutationError) => {
      setError(formatRequestError(
        mutationError,
        'The Gmail send outcome could not be confirmed.',
      ))
      try {
        const latest = await actionApprovalService.getProposal(
          proposal.proposal_id,
        )
        updateProposalMessage(message, latest, onUpdated)
      } catch (statusError) {
        setError(
          `${formatRequestError(
            mutationError,
            'The Gmail send outcome could not be confirmed.',
          )} Status check failed: ${formatRequestError(
            statusError,
            'Check the send status before taking further action.',
          )}`,
        )
      }
    },
    onSettled: () => {
      requestInProgress.current = false
    },
  })

  const statusMutation = useMutation({
    mutationFn: () =>
      actionApprovalService.getProposal(proposal.proposal_id),
    onSuccess: (latest) => {
      setError('')
      updateProposalMessage(message, latest, onUpdated)
    },
    onError: (mutationError) => {
      setError(formatRequestError(
        mutationError,
        'Unable to refresh the Gmail send status.',
      ))
    },
    onSettled: () => {
      requestInProgress.current = false
    },
  })

  const isExpired = proposal.status === 'pending' && expired
  const visibleStatus = isExpired ? 'expired' : proposal.status
  const isPending = visibleStatus === 'pending'
  const approvalStatus = ['pending', 'approved', 'rejected', 'expired']
    .includes(visibleStatus)
    ? visibleStatus
    : 'approved'
  const executionStatus = visibleStatus === 'approved'
    ? 'not started'
    : ['executing', 'completed', 'failed', 'execution_uncertain']
      .includes(visibleStatus)
      ? visibleStatus.replaceAll('_', ' ')
      : 'not started'
  function resolveProposal(decision) {
    if (requestInProgress.current || mutation.isPending) return
    requestInProgress.current = true
    mutation.mutate(decision)
  }
  function executeProposal() {
    if (
      requestInProgress.current ||
      mutation.isPending ||
      executeMutation.isPending ||
      proposal.action_type !== 'gmail.send' ||
      proposal.status !== 'approved'
    ) {
      return
    }
    requestInProgress.current = true
    setError('')
    executeMutation.mutate()
  }
  function refreshExecutionStatus() {
    if (requestInProgress.current || statusMutation.isPending) return
    requestInProgress.current = true
    statusMutation.mutate()
  }
  const statusStyles = {
    pending: isDark
      ? 'bg-amber-500/10 text-amber-300'
      : 'bg-amber-50 text-amber-700',
    approved: isDark
      ? 'bg-emerald-500/10 text-emerald-300'
      : 'bg-emerald-50 text-emerald-700',
    rejected: isDark
      ? 'bg-slate-500/10 text-slate-300'
      : 'bg-slate-100 text-slate-600',
    expired: isDark
      ? 'bg-slate-500/10 text-slate-300'
      : 'bg-slate-100 text-slate-600',
    executing: isDark
      ? 'bg-violet-500/10 text-violet-300'
      : 'bg-violet-50 text-violet-700',
    completed: isDark
      ? 'bg-emerald-500/10 text-emerald-300'
      : 'bg-emerald-50 text-emerald-700',
    failed: isDark
      ? 'bg-rose-500/10 text-rose-300'
      : 'bg-rose-50 text-rose-700',
    execution_uncertain: isDark
      ? 'bg-rose-500/10 text-rose-300'
      : 'bg-rose-50 text-rose-700',
  }[visibleStatus]

  return (
    <section
      aria-label="Action approval proposal"
      className={`mt-4 overflow-hidden rounded-xl border text-left shadow-sm ${
        isDark
          ? 'border-amber-500/25 bg-[#191b29]'
          : 'border-amber-200 bg-white'
      }`}
    >
      <header className={`flex flex-wrap items-center justify-between gap-3 border-b px-4 py-3 ${
        isDark
          ? 'border-[#303245] bg-amber-500/5'
          : 'border-amber-100 bg-amber-50/70'
      }`}>
        <div>
          <h3 className="text-sm font-semibold">Review proposed action</h3>
          <p className={`mt-0.5 text-xs ${isDark ? 'text-slate-400' : 'text-slate-500'}`}>
            {proposal.action_type} · Approval and execution are separate
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <span className={`rounded-full px-2.5 py-1 text-[11px] font-semibold ${statusStyles}`}>
            Approval: {approvalStatus}
          </span>
          <span className={`rounded-full px-2.5 py-1 text-[11px] font-semibold capitalize ${statusStyles}`}>
            Execution: {executionStatus}
          </span>
        </div>
      </header>

      <div className="space-y-3 p-4">
        <div className={`rounded-lg px-3 py-2 text-xs ${
          isDark ? 'bg-[#171a27] text-slate-300' : 'bg-slate-50 text-slate-700'
        }`}>
          <span className="font-semibold">Account: </span>
          {proposal.account_email || 'Google account'}
          <span className="ml-1 font-mono text-[10px] opacity-70">
            ({proposal.connection_id})
          </span>
        </div>

        <dl className={`divide-y text-xs ${
          isDark ? 'divide-[#303245]' : 'divide-slate-100'
        }`}>
          {Object.entries(proposal.parameters).map(([key, value]) => (
            <div key={key} className="grid gap-1 py-2 sm:grid-cols-[110px_1fr]">
              <dt className={`font-semibold ${isDark ? 'text-slate-400' : 'text-slate-500'}`}>
                {key.replaceAll('_', ' ')}
              </dt>
              <dd className={`break-words whitespace-pre-wrap ${
                isDark ? 'text-slate-200' : 'text-slate-700'
              }`}>
                {displayValue(value)}
              </dd>
            </div>
          ))}
        </dl>

        <p className={`text-[11px] ${isDark ? 'text-slate-500' : 'text-slate-400'}`}>
          Expires {new Date(proposal.expires_at).toLocaleString()}
        </p>

        {error && (
          <p role="alert" className="text-xs text-rose-400">
            {error}
          </p>
        )}

        {visibleStatus === 'approved' && (
          <p role="status" className="text-xs text-emerald-500">
            Approved by you. No email will be sent until you explicitly choose Execute.
          </p>
        )}
        {visibleStatus === 'executing' && (
          <p role="status" className="text-xs text-violet-400">
            The send was claimed. If the worker was interrupted, recovery will mark it uncertain; it will not be sent again automatically.
          </p>
        )}
        {['executing', 'execution_uncertain'].includes(visibleStatus) && (
          <div className="flex justify-end">
            <button
              type="button"
              onClick={refreshExecutionStatus}
              disabled={statusMutation.isPending}
              className={`rounded-lg border px-3 py-2 text-xs font-semibold disabled:cursor-not-allowed disabled:opacity-50 ${
                isDark
                  ? 'border-[#34384b] text-slate-300 hover:bg-white/5'
                  : 'border-slate-200 text-slate-700 hover:bg-slate-50'
              }`}
            >
              {statusMutation.isPending ? 'Checking...' : 'Check send status'}
            </button>
          </div>
        )}
        {visibleStatus === 'completed' && (
          <p role="status" className="text-xs text-emerald-500">
            Gmail confirmed the message was sent.
            {proposal.result?.gmail_message_id && (
              <span className="ml-1 font-mono">
                Message ID: {proposal.result.gmail_message_id}
              </span>
            )}
          </p>
        )}
        {visibleStatus === 'failed' && (
          <p role="alert" className="text-xs text-rose-400">
            Gmail rejected the send request. This proposal cannot be retried.
          </p>
        )}
        {visibleStatus === 'execution_uncertain' && (
          <p role="alert" className="text-xs text-rose-400">
            Gmail may have sent the message, but the result is unconfirmed. Do not retry this proposal.
          </p>
        )}
        {isExpired && (
          <p role="status" className="text-xs text-slate-400">
            This proposal has expired and cannot be approved.
          </p>
        )}

        {isPending && (
          <div className="flex flex-col-reverse gap-2 border-t pt-3 sm:flex-row sm:justify-end">
            <button
              type="button"
              onClick={() => resolveProposal('reject')}
              disabled={mutation.isPending}
              className={`rounded-lg border px-4 py-2 text-xs font-semibold disabled:cursor-not-allowed disabled:opacity-50 ${
                isDark
                  ? 'border-[#34384b] text-slate-300 hover:bg-white/5'
                  : 'border-slate-200 text-slate-700 hover:bg-slate-50'
              }`}
            >
              {mutation.isPending && mutation.variables === 'reject'
                ? 'Rejecting...'
                : 'Reject'}
            </button>
            <button
              type="button"
              onClick={() => resolveProposal('approve')}
              disabled={mutation.isPending}
              className="rounded-lg bg-violet-600 px-4 py-2 text-xs font-semibold text-white transition hover:bg-violet-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {mutation.isPending && mutation.variables === 'approve'
                ? 'Approving...'
                : 'Approve'}
            </button>
          </div>
        )}
        {proposal.status === 'approved' && proposal.action_type === 'gmail.send' && (
          <div className={`flex justify-end border-t pt-3 ${
            isDark ? 'border-[#303245]' : 'border-slate-100'
          }`}>
            <button
              type="button"
              onClick={executeProposal}
              disabled={mutation.isPending || executeMutation.isPending}
              className="rounded-lg bg-violet-600 px-4 py-2 text-xs font-semibold text-white transition hover:bg-violet-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {executeMutation.isPending ? 'Sending...' : 'Execute Gmail send'}
            </button>
          </div>
        )}
      </div>
    </section>
  )
}

export default ActionApprovalCard
