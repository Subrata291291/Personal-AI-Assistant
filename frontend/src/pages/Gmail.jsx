
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  useInfiniteQuery,
  useQuery,
  useMutation,
  useQueryClient,
} from '@tanstack/react-query'
import { useOutletContext } from 'react-router-dom'

import { api } from '../lib/api.js'
import { gmailService } from '../services/gmailService.js'
import { useAuth } from '../context/AuthContext'

const CACHE_TIME = 5 * 60 * 1000
const GC_TIME = 30 * 60 * 1000

const FOLDERS = [
  { id: 'inbox', label: 'Inbox', icon: '📥' },
  { id: 'starred', label: 'Starred', icon: '⭐' },
  { id: 'important', label: 'Important', icon: '❗' },
  { id: 'sent', label: 'Sent', icon: '📤' },
  { id: 'drafts', label: 'Drafts', icon: '📝' },
  { id: 'all', label: 'All Mail', icon: '📧' },
  { id: 'spam', label: 'Spam', icon: '🚫' },
  { id: 'trash', label: 'Trash', icon: '🗑️' },
]

function formatDate(value) {
  if (!value) return ''

  const date = new Date(value)

  if (Number.isNaN(date.getTime())) return value

  return date.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  })
}

function formatFullDate(value) {
  if (!value) return 'Not available'

  const date = new Date(value)

  if (Number.isNaN(date.getTime())) return value

  return date.toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

function getInitials(sender) {
  if (!sender) return '?'

  const match = sender.match(/^([^<]+)</)
  const name = (match?.[1] || sender).trim()

  return name
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join('')
}

function getAttachmentName(attachment) {
  return (
    attachment.filename ||
    attachment.name ||
    'Unnamed attachment'
  )
}

function GmailActionSpinner() {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      className="h-[17px] w-[17px] animate-spin"
    >
      <circle
        cx="12"
        cy="12"
        r="9"
        stroke="currentColor"
        strokeOpacity="0.25"
        strokeWidth="2"
      />
      <path
        d="M21 12a9 9 0 0 0-9-9"
        stroke="currentColor"
        strokeLinecap="round"
        strokeWidth="2"
      />
    </svg>
  )
}

export default function Gmail() {
  const queryClient = useQueryClient()
  const { isDark } = useOutletContext()
  const { user } = useAuth()

  const userId = user?.id

  const [connectionId, setConnectionId] = useState('')
  const [activeFolder, setActiveFolder] = useState('inbox')

  const [searchInput, setSearchInput] = useState('')
  const [searchQuery, setSearchQuery] = useState('')

  const [selectedMessageId, setSelectedMessageId] = useState(null)
  const [composeMode, setComposeMode] = useState('compose')
  const [showCompose, setShowCompose] = useState(false)
  const [composeData, setComposeData] = useState({
    to: '',
    cc: '',
    bcc: '',
    subject: '',
    body: '',
  })
  const [composeError, setComposeError] = useState('')
  const [composeSuccess, setComposeSuccess] = useState('')
  const [showHtmlBody, setShowHtmlBody] = useState(true)
  const [downloadingAttachmentId, setDownloadingAttachmentId] = useState(null)
  const [attachmentDownloadError, setAttachmentDownloadError] = useState('')
  const [manualRefreshing, setManualRefreshing] = useState(false)
  const [trashNotice, setTrashNotice] = useState(null)
  const [messageActionConfirmation, setMessageActionConfirmation] = useState(null)
  const trashRequestInFlight = useRef(false)

  const colors = {
    card: isDark
      ? 'border-[#252838] bg-[#11131c]'
      : 'border-slate-200 bg-white',

    heading: isDark ? 'text-slate-100' : 'text-slate-800',

    text: isDark ? 'text-slate-300' : 'text-slate-700',

    muted: isDark ? 'text-slate-400' : 'text-slate-500',

    input: isDark
      ? 'border-[#303448] bg-[#171a27] text-slate-100 placeholder:text-slate-500'
      : 'border-slate-200 bg-slate-50 text-slate-800 placeholder:text-slate-400',

    hover: isDark ? 'hover:bg-[#191c29]' : 'hover:bg-slate-50',

    label: isDark
      ? 'bg-[#252838] text-slate-400'
      : 'bg-slate-100 text-slate-600',

    skeleton: isDark ? 'bg-[#202333]' : 'bg-slate-100',

    divider: isDark
      ? 'border-[#252838]'
      : 'border-slate-200',

    selectedFolder: isDark
      ? 'bg-violet-500/15 text-violet-300'
      : 'bg-violet-50 text-violet-700',

    folder: isDark
      ? 'text-slate-400 hover:bg-[#191c29] hover:text-slate-100'
      : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900',
  }

  // Connected Google accounts
  const connectionsQuery = useQuery({
    queryKey: ['gmail-connections', userId],

    queryFn: () => gmailService.getConnections(),

    enabled: Boolean(userId),

    staleTime: CACHE_TIME,
    gcTime: GC_TIME,

    refetchOnMount: false,
    refetchOnWindowFocus: false,
  })

  const connections = connectionsQuery.data || []

  const googleConnections = useMemo(
    () =>
      connections.filter(
        (item) =>
          item.provider === 'google' &&
          item.status === 'connected',
      ),
    [connections],
  )

  const selectedConnection = useMemo(
    () =>
      googleConnections.find(
        (item) => item.id === connectionId,
      ),
    [googleConnections, connectionId],
  )

  const sendEmailMutation = useMutation({
    mutationFn: ({ mode, ...payload }) => {
      if (mode === 'reply') {
        return gmailService.replyMessage(payload)
      }

      if (mode === 'reply-all') {
        return gmailService.replyAllMessage(payload)
      }

      if (mode === 'forward') {
        return gmailService.forwardMessage(payload)
      }

      return gmailService.sendMessage(payload)
    },

    onSuccess: (_result, variables) => {
      const affectedFolders = new Set(['sent', 'all'])

      if (variables.messageId && variables.activeFolder) {
        affectedFolders.add(variables.activeFolder)
      }

      for (const folder of affectedFolders) {
        queryClient.invalidateQueries({
          queryKey: [
            'gmail-messages',
            variables.userId,
            variables.connectionId,
            folder,
          ],
        })
      }

      if (variables.messageId) {
        queryClient.invalidateQueries({
          queryKey: [
            'gmail-message',
            variables.userId,
            variables.connectionId,
            variables.messageId,
          ],
          exact: true,
        })
      }

      const successMessage = {
        compose: 'Email sent successfully!',
        reply: 'Reply sent successfully!',
        'reply-all': 'Reply All sent successfully!',
        forward: 'Email forwarded successfully!',
      }

      setComposeSuccess(
        successMessage[composeMode] || 'Email sent successfully!'
      )

      setComposeData({
        to: '',
        cc: '',
        bcc: '',
        subject: '',
        body: '',
      })

      setComposeMode('compose')
      setComposeError('')
      setShowCompose(false)
    },

    onError: (error) => {
      setComposeError(error.message || 'Unable to send email.')
    },
  })

  const trashMessageMutation = useMutation({
    mutationFn: ({ action, connectionId: targetConnectionId, messageId }) => {
      if (action === 'permanent') {
        return gmailService.permanentlyDeleteMessage({
          connectionId: targetConnectionId,
          messageId,
        })
      }

      if (action === 'restore') {
        return gmailService.restoreMessage({
          connectionId: targetConnectionId,
          messageId,
        })
      }

      if (action === 'archive') {
        return gmailService.archiveMessage({
          connectionId: targetConnectionId,
          messageId,
        })
      }

      return gmailService.trashMessage({
        connectionId: targetConnectionId,
        messageId,
      })
    },

    onMutate: async ({ action, userId: targetUserId, connectionId: targetConnectionId, messageId }) => {
      const queryKey = [
        'gmail-messages',
        targetUserId,
        targetConnectionId,
      ]

      await queryClient.cancelQueries({ queryKey })

      const previousQueries = queryClient.getQueriesData({ queryKey })
      const message = previousQueries
        .flatMap(([, data]) =>
          data?.pages?.flatMap((page) => page.messages || []) || [],
        )
        .find((item) => item.id === messageId)

      for (const [cachedQueryKey, previousData] of previousQueries) {
        const folder = cachedQueryKey[3]
        if (
          !previousData?.pages
        ) {
          continue
        }

        queryClient.setQueryData(cachedQueryKey, {
          ...previousData,
          pages: previousData.pages.map((page) => ({
            ...page,
            messages: (page.messages || [])
              .filter((item) => {
                if (action === 'permanent') return item.id !== messageId
                if (action === 'trash') {
                  return folder === 'trash' || item.id !== messageId
                }
                if (action === 'restore') {
                  return folder !== 'trash' || item.id !== messageId
                }
                if (action === 'archive') {
                  return folder !== 'inbox' || item.id !== messageId
                }
                return true
              })
              .map((item) => {
                if (item.id !== messageId || !message) return item
                if (action === 'archive') {
                  return {
                    ...item,
                    labels: (item.labels || []).filter(
                      (label) => label !== 'INBOX',
                    ),
                  }
                }
                if (action === 'restore') {
                  return {
                    ...item,
                    labels: (item.labels || []).filter(
                      (label) => label !== 'TRASH',
                    ),
                  }
                }
                return item
              }),
          })),
        })
      }

      return { previousQueries, queryKey, message }
    },

    onError: (error, variables, context) => {
      for (const [queryKey, previousData] of context?.previousQueries || []) {
        queryClient.setQueryData(queryKey, previousData)
      }

      setTrashNotice({
        type: 'error',
        message:
          error.message ||
          ({
            permanent: 'Unable to permanently delete this email.',
            restore: 'Unable to restore this email.',
            archive: 'Unable to archive this email.',
            trash: 'Unable to move this email to Trash.',
          }[variables.action] || 'Unable to update this email.'),
      })
    },

    onSuccess: (result, variables, context) => {
      if (variables.action === 'permanent') {
        queryClient.removeQueries({
          queryKey: [
            'gmail-message',
            variables.userId,
            variables.connectionId,
            variables.messageId,
          ],
          exact: true,
        })
      } else if (variables.action === 'trash') {
        queryClient.invalidateQueries({
          queryKey: [...(context?.queryKey || []), 'trash'],
          refetchType: 'none',
        })
      } else if (variables.action === 'restore') {
        const labels = result.label_ids || []
        const restoredMessage = context?.message
          ? {
              ...context.message,
              labels,
            }
          : null
        const destinationFolders = new Set(['all'])

        if (labels.includes('INBOX')) destinationFolders.add('inbox')
        if (labels.includes('STARRED')) destinationFolders.add('starred')
        if (labels.includes('IMPORTANT')) destinationFolders.add('important')
        if (labels.includes('SENT')) destinationFolders.add('sent')
        if (labels.includes('DRAFT')) destinationFolders.add('drafts')
        if (labels.includes('SPAM')) destinationFolders.add('spam')

        for (const [cachedQueryKey, previousData] of context?.previousQueries || []) {
          const folder = cachedQueryKey[3]
          const search = cachedQueryKey[4]
          if (!previousData?.pages || !destinationFolders.has(folder)) continue

          if (search) {
            queryClient.invalidateQueries({
              queryKey: cachedQueryKey,
              exact: true,
              refetchType: 'all',
            })
            continue
          }

          const alreadyPresent = previousData.pages.some((page) =>
            (page.messages || []).some(
              (message) => message.id === variables.messageId,
            ),
          )
          if (!restoredMessage) continue

          queryClient.setQueryData(cachedQueryKey, (currentData) => ({
            ...currentData,
            pages: currentData.pages.map((page, index) =>
              index === 0 && !alreadyPresent
                ? {
                    ...page,
                    messages: [
                      restoredMessage,
                      ...(page.messages || []).filter(
                        (message) => message.id !== variables.messageId,
                      ),
                    ],
                  }
                : {
                    ...page,
                      messages: (page.messages || []).map((message) =>
                        message.id === variables.messageId
                          ? { ...message, labels }
                          : message,
                      ),
                  },
            ),
          }))
        }
      } else if (variables.action === 'archive') {
        const labels = result.label_ids || []
        for (const [cachedQueryKey] of context?.previousQueries || []) {
          queryClient.setQueryData(cachedQueryKey, (currentData) => {
            if (!currentData?.pages) return currentData
            return {
              ...currentData,
              pages: currentData.pages.map((page) => ({
                  ...page,
                  messages: (page.messages || []).map((message) =>
                    message.id === variables.messageId
                      ? { ...message, labels }
                      : message,
                  ),
              })),
            }
          })
        }

        queryClient.invalidateQueries({
          queryKey: [
            'gmail-messages',
            variables.userId,
            variables.connectionId,
            'inbox',
          ],
          refetchType: 'none',
        })
      }

      const successMessages = {
        permanent: 'Email permanently deleted.',
        restore: 'Email restored from Trash.',
        archive: 'Email archived.',
        trash: 'Email moved to Trash.',
      }
      setTrashNotice({
        type: 'success',
        message: successMessages[variables.action],
      })
      setMessageActionConfirmation(null)
    },

    onSettled: () => {
      trashRequestInFlight.current = false
    },
  })

  const reconnectGoogleMutation = useMutation({
    mutationFn: () => api.post('/api/v1/oauth/google/connect'),

    onSuccess: (result) => {
      if (!result?.authorization_url) {
        setTrashNotice({
          type: 'error',
          message: 'Google authorization URL was not returned.',
        })
        return
      }

      window.location.assign(result.authorization_url)
    },

    onError: (error) => {
      setTrashNotice({
        type: 'error',
        message: error.message || 'Unable to reconnect the Google account.',
      })
    },
  })

  // Restore selected account after navigating between tabs.
  useEffect(() => {
    if (!userId || googleConnections.length === 0) return

    const storageKey = `gmail-selected-account-${userId}`

    const savedConnectionId = sessionStorage.getItem(storageKey)

    const savedConnectionExists = googleConnections.some(
      (item) => item.id === savedConnectionId,
    )

    const nextConnectionId = savedConnectionExists
      ? savedConnectionId
      : googleConnections[0].id

    setConnectionId(nextConnectionId)

    sessionStorage.setItem(storageKey, nextConnectionId)
  }, [userId, googleConnections])

  function handleConnectionChange(nextId) {
    setConnectionId(nextId)

    if (userId) {
      sessionStorage.setItem(
        `gmail-selected-account-${userId}`,
        nextId,
      )
    }

    closeMessageDetails()
  }

  // Folder selection
  function handleFolderChange(folderId) {
    setActiveFolder(folderId)
    closeMessageDetails()
  }

  // Gmail inbox with folder-specific cached pagination
  const messagesQuery = useInfiniteQuery({
    queryKey: [
      'gmail-messages',
      userId,
      connectionId,
      activeFolder,
      searchQuery,
    ],

    enabled: Boolean(userId && connectionId),

    initialPageParam: undefined,

    queryFn: ({ pageParam }) =>
      gmailService.getMessages({
        connectionId,
        maxResults: 20,
        pageToken: pageParam,
        query: searchQuery,
        folder: activeFolder,
      }),

    getNextPageParam: (lastPage) =>
      lastPage.next_page_token || undefined,

    staleTime: CACHE_TIME,
    gcTime: GC_TIME,

    refetchOnMount: false,
    refetchOnWindowFocus: false,
  })

  const messages = useMemo(
    () =>
      messagesQuery.data?.pages.flatMap(
        (page) => page.messages || [],
      ) || [],
    [messagesQuery.data],
  )

  // Individual email details query
  const messageDetailQuery = useQuery({
    queryKey: [
      'gmail-message',
      userId,
      connectionId,
      selectedMessageId,
    ],

    queryFn: () =>
      gmailService.getMessage({
        connectionId,
        messageId: selectedMessageId,
      }),

    enabled: Boolean(
      userId && connectionId && selectedMessageId,
    ),

    staleTime: CACHE_TIME,
    gcTime: GC_TIME,

    refetchOnMount: false,
    refetchOnWindowFocus: false,
  })

  function handleSearch(event) {
    event.preventDefault()
    setSearchQuery(searchInput.trim())
    setTrashNotice(null)
  }

  function clearSearch() {
    setSearchInput('')
    setSearchQuery('')
    setTrashNotice(null)
  }

  async function handleManualRefresh() {
    setManualRefreshing(true)

    try {
      await messagesQuery.refetch()
    } finally {
      setManualRefreshing(false)
    }
  }

  function handleMessageAction(event, message, action) {
    event.stopPropagation()

    if (
      trashMessageMutation.isPending ||
      trashRequestInFlight.current ||
      !userId ||
      !connectionId
    ) {
      return
    }

    setTrashNotice(null)
    setMessageActionConfirmation({
      messageId: message.id,
      subject: message.subject,
      userId,
      connectionId,
      action,
    })
  }

  function closeMessageActionConfirmation() {
    if (trashMessageMutation.isPending || trashRequestInFlight.current) return
    setMessageActionConfirmation(null)
  }

  function confirmMessageAction() {
    if (
      trashMessageMutation.isPending ||
      trashRequestInFlight.current ||
      !messageActionConfirmation
    ) {
      return
    }

    trashRequestInFlight.current = true
    trashMessageMutation.mutate({
      userId: messageActionConfirmation.userId,
      connectionId: messageActionConfirmation.connectionId,
      messageId: messageActionConfirmation.messageId,
      action: messageActionConfirmation.action,
    })
  }

  function openMessageDetails(messageId) {
    setSelectedMessageId(messageId)
    setShowHtmlBody(true)
  }

  function closeMessageDetails() {
    setSelectedMessageId(null)
    setShowHtmlBody(true)
    setAttachmentDownloadError('')
    setDownloadingAttachmentId(null)
  }

  async function handleAttachmentDownload(attachment) {
    const attachmentId = attachment.attachment_id

    if (!connectionId || !selectedMessageId || !attachmentId) {
      setAttachmentDownloadError('Attachment information is incomplete.')
      return
    }

    try {
      setDownloadingAttachmentId(attachmentId)
      setAttachmentDownloadError('')

      const blob = await gmailService.downloadAttachment({
        connectionId,
        messageId: selectedMessageId,
        attachmentId,
      })

      const filename = getAttachmentName(attachment)
        .replace(/[\\/:*?"<>|]/g, '_')
        .trim() || 'attachment'

      const objectUrl = URL.createObjectURL(blob)
      const link = document.createElement('a')

      link.href = objectUrl
      link.download = filename

      document.body.appendChild(link)
      link.click()
      link.remove()

      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
    } catch (error) {
      setAttachmentDownloadError(
        error.message || 'Unable to download attachment.'
      )
    } finally {
      setDownloadingAttachmentId(null)
    }
  }

  function handleComposeChange(field, value) {
    setComposeData((previous) => ({
      ...previous,
      [field]: value,
    }))
  }

  function openCompose(mode = 'compose') {
    setComposeMode(mode)
    setComposeError('')
    setComposeSuccess('')

    setComposeData({
      to: '',
      cc: '',
      bcc: '',
      subject: '',
      body: '',
    })

    setShowCompose(true)
  }

  function parseRecipients(value) {
    return value
      .split(',')
      .map((email) => email.trim())
      .filter(Boolean)
  }

  function handleSendEmail(event) {
    event.preventDefault()

    setComposeError('')
    setComposeSuccess('')

    if (!connectionId) {
      setComposeError('Please select a Google account.')
      return
    }

    if (
      composeMode !== 'forward' &&
      !composeData.body.trim()
    ) {
      setComposeError('Enter an email message.')
      return
    }

    const commonPayload = {
      userId,
      connectionId,
      messageId: selectedMessageId,
      activeFolder,
      body: composeData.body,
    }

    // Reply and Reply All
    if (composeMode === 'reply' || composeMode === 'reply-all') {
      const action = composeMode === 'reply' ? 'Reply' : 'Reply All'

      if (!window.confirm(`${action} to this email?`)) {
        return
      }

      sendEmailMutation.mutate({
        mode: composeMode,
        ...commonPayload,
      })

      return
    }

    // Compose and Forward
    const recipients = parseRecipients(composeData.to)
    const cc = parseRecipients(composeData.cc)
    const bcc = parseRecipients(composeData.bcc)

    if (recipients.length === 0) {
      setComposeError('Enter at least one recipient.')
      return
    }

    if (
      composeMode === 'compose' &&
      !composeData.subject.trim()
    ) {
      setComposeError('Enter an email subject.')
      return
    }

    const action = composeMode === 'forward' ? 'Forward' : 'Send'

    const confirmed = window.confirm(
      `${action} email to ${recipients.join(', ')}?`
    )

    if (!confirmed) return

    if (composeMode === 'forward') {
      sendEmailMutation.mutate({
        mode: 'forward',
        ...commonPayload,
        to: recipients,
        cc,
        bcc,
      })
    } else {
      sendEmailMutation.mutate({
        mode: 'compose',
        userId,
        connectionId,
        to: recipients,
        cc,
        bcc,
        subject: composeData.subject.trim(),
        body: composeData.body,
      })
    }
  }

  // Close modal using Escape key
  useEffect(() => {
    if (!selectedMessageId) return

    function handleKeyDown(event) {
      if (event.key === 'Escape') {
        closeMessageDetails()
      }
    }

    window.addEventListener('keydown', handleKeyDown)

    return () => {
      window.removeEventListener('keydown', handleKeyDown)
    }
  }, [selectedMessageId])

  useEffect(() => {
    if (!messageActionConfirmation) return

    function handleKeyDown(event) {
      if (
        event.key === 'Escape' &&
        !trashMessageMutation.isPending &&
        !trashRequestInFlight.current
      ) {
        setMessageActionConfirmation(null)
      }
    }

    window.addEventListener('keydown', handleKeyDown)

    return () => {
      window.removeEventListener('keydown', handleKeyDown)
    }
  }, [messageActionConfirmation, trashMessageMutation.isPending])

  const selectedMessage = messageDetailQuery.data?.message

  const emailBody = selectedMessage?.body || {}

  const attachments = selectedMessage?.attachments || []

  const hasHtml = Boolean(emailBody.html)
  const hasPlainText = Boolean(emailBody.plain_text || emailBody.text)

  const activeFolderInfo = FOLDERS.find(
    (folder) => folder.id === activeFolder,
  )

  // Sandboxed HTML email document
  const safeHtmlDocument = emailBody.html
    ? `<!doctype html>
       <html>
         <head>
           <meta charset="UTF-8" />

           <meta
             http-equiv="Content-Security-Policy"
             content="default-src 'none'; img-src https: data: cid:; style-src 'unsafe-inline' https:; font-src https: data:; script-src 'none'; form-action 'none';"
           />

           <meta
             name="viewport"
             content="width=device-width, initial-scale=1"
           />

           <style>
             * {
               box-sizing: border-box;
             }

             html {
               scrollbar-width: thin;
               scrollbar-color: #a1a1aa transparent;
             }

             body {
               font-family: Arial, sans-serif;
               font-size: 14px;
               line-height: 1.6;
               overflow-wrap: anywhere;
               color: #222;
               padding: 16px;
               margin: 0;
             }

             img {
               max-width: 100%;
               height: auto;
             }

             a {
               overflow-wrap: anywhere;
             }

             pre {
               white-space: pre-wrap;
             }

             table {
               max-width: 100%;
             }

             ::-webkit-scrollbar {
               width: 8px;
               height: 8px;
             }

             ::-webkit-scrollbar-track {
               background: transparent;
             }

             ::-webkit-scrollbar-thumb {
               background: #a1a1aa;
               border-radius: 20px;
             }

             ::-webkit-scrollbar-thumb:hover {
               background: #71717a;
             }
           </style>
         </head>

         <body>${emailBody.html}</body>
       </html>`
    : ''

  const loading = messagesQuery.isLoading && !messagesQuery.data
  const error = messagesQuery.error

  return (
    <div className="gmail-page space-y-6">

      {/* Custom scrollbar styling */}
      <style>{`
        .gmail-page .gmail-scrollbar {
          scrollbar-width: thin;
          scrollbar-color: ${isDark ? '#555b70 transparent' : '#c0c5d0 transparent'};
        }

        .gmail-page .gmail-scrollbar::-webkit-scrollbar {
          width: 8px;
          height: 8px;
        }

        .gmail-page .gmail-scrollbar::-webkit-scrollbar-track {
          background: transparent;
        }

        .gmail-page .gmail-scrollbar::-webkit-scrollbar-thumb {
          background: ${isDark ? '#555b70' : '#c0c5d0'};
          border-radius: 20px;
          border: 2px solid transparent;
          background-clip: padding-box;
        }

        .gmail-page .gmail-scrollbar::-webkit-scrollbar-thumb:hover {
          background: ${isDark ? '#737b94' : '#969eae'};
          background-clip: padding-box;
        }
      `}</style>

      {/* Page heading */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">

        <div className="flex items-center gap-3">

          <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-red-500/10 text-2xl text-red-400">
            ✉
          </div>

          <div>
            <h1 className={`text-2xl font-bold ${colors.heading}`}>
              Gmail
            </h1>

            <p className={`mt-1 text-sm ${colors.muted}`}>
              Manage and read your connected Gmail account.
            </p>
          </div>

        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => openCompose('compose')}
            disabled={!connectionId}
            className="rounded-xl bg-violet-600 px-5 py-3 text-sm font-semibold text-white transition hover:bg-violet-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            ✉ Compose
          </button>

          <div className="flex items-center gap-2 rounded-full border border-emerald-500/20 bg-emerald-500/10 px-3 py-2 text-xs font-semibold text-emerald-500">
            <span className="h-2 w-2 rounded-full bg-emerald-400" />
            Read, Send &amp; Manage access
          </div>
        </div>

      </div>

      {/* Connected account selector */}
      <section className={`rounded-2xl border p-5 ${colors.card}`}>

        <label
          htmlFor="gmail-account"
          className={`mb-2 block text-sm font-semibold ${colors.text}`}
        >
          Connected Google account
        </label>

        {connectionsQuery.isLoading ? (
          <p className={`text-sm ${colors.muted}`}>
            Loading connected accounts...
          </p>
        ) : connectionsQuery.isError ? (
          <div className="text-sm text-rose-500">
            {connectionsQuery.error.message}

            <button
              type="button"
              onClick={() => connectionsQuery.refetch()}
              className="ml-2 underline"
            >
              Retry
            </button>
          </div>
        ) : (
          <select
            id="gmail-account"
            value={connectionId}
            onChange={(event) =>
              handleConnectionChange(event.target.value)
            }
            className={`w-full rounded-xl border px-4 py-3 text-sm outline-none focus:border-violet-500 sm:max-w-md ${colors.input}`}
          >
            <option value="">Select a Google account</option>

            {googleConnections.map((account) => (
              <option key={account.id} value={account.id}>
                {account.email || 'Google account'}
              </option>
            ))}
          </select>
        )}

        {selectedConnection && (
          <p className="mt-3 text-xs text-emerald-500">
            Connected: {selectedConnection.email}
          </p>
        )}

      </section>

      {/* Main Gmail layout */}
      <section className={`overflow-hidden rounded-2xl border ${colors.card}`}>

        <div className="grid min-h-[620px] grid-cols-1 md:grid-cols-[190px_minmax(0,1fr)]">

          {/* Folder sidebar */}
          <aside
            className={`border-b p-3 md:border-b-0 md:border-r ${
              colors.divider
            }`}
          >

            <div className="mb-3 flex items-center justify-between px-3">
              <h2 className={`text-sm font-bold ${colors.heading}`}>
                Mail folders
              </h2>
            </div>

            <nav
              aria-label="Gmail folders"
              className="gmail-scrollbar flex gap-2 overflow-x-auto pb-2 md:flex-col md:overflow-x-hidden md:overflow-y-auto md:pb-0"
            >

              {FOLDERS.map((folder) => {
                const active = activeFolder === folder.id

                return (
                  <button
                    key={folder.id}
                    type="button"
                    onClick={() => handleFolderChange(folder.id)}
                    aria-current={active ? 'page' : undefined}
                    className={`flex shrink-0 items-center gap-3 rounded-xl px-3 py-3 text-left text-sm font-medium transition md:w-full ${
                      active
                        ? colors.selectedFolder
                        : colors.folder
                    }`}
                  >

                    <span className="text-base">
                      {folder.icon}
                    </span>

                    <span className="whitespace-nowrap">
                      {folder.label}
                    </span>

                    {active && (
                      <span className="ml-auto h-1.5 w-1.5 rounded-full bg-violet-500" />
                    )}

                  </button>
                )
              })}

            </nav>

            <div className={`mt-5 hidden border-t pt-4 md:block ${colors.divider}`}>
              <p className={`px-3 text-xs ${colors.muted}`}>
                Connected account
              </p>

              <p className={`mt-2 break-all px-3 text-xs font-medium ${colors.text}`}>
                {selectedConnection?.email || 'No account selected'}
              </p>
            </div>

          </aside>

          {/* Inbox content */}
          <div className="min-w-0">

            {/* Inbox toolbar */}
            <div
              className={`flex flex-col gap-4 border-b p-5 ${
                colors.divider
              }`}
            >

              <div className="flex items-center justify-between gap-3">

                <div>
                  <h2 className={`text-lg font-bold ${colors.heading}`}>
                    {activeFolderInfo?.icon} {activeFolderInfo?.label}
                  </h2>

                  <p className={`mt-1 text-xs ${colors.muted}`}>
                    {messages.length} messages loaded
                  </p>
                </div>

                <button
                  type="button"
                  disabled={!connectionId || manualRefreshing}
                  onClick={handleManualRefresh}
                  className={`rounded-lg border px-3 py-2 text-xs font-semibold transition disabled:opacity-50 ${
                    colors.input
                  }`}
                >
                  {manualRefreshing ? 'Refreshing...' : '↻ Refresh'}
                </button>

              </div>

              {trashNotice && (
                <p
                  role={trashNotice.type === 'error' ? 'alert' : 'status'}
                  className={`px-5 pt-4 text-sm ${
                    trashNotice.type === 'error'
                      ? 'text-rose-500'
                      : 'text-emerald-500'
                  }`}
                >
                  {trashNotice.message}
                </p>
              )}

              {/* Search */}
              <form
                onSubmit={handleSearch}
                className="flex w-full gap-2"
              >

                <input
                  value={searchInput}
                  onChange={(event) =>
                    setSearchInput(event.target.value)
                  }
                  disabled={!connectionId}
                  placeholder={`Search in ${activeFolderInfo?.label || 'Gmail'}...`}
                  className={`min-w-0 flex-1 rounded-xl border px-4 py-2.5 text-sm outline-none focus:border-violet-500 disabled:opacity-50 ${colors.input}`}
                />

                <button
                  type="submit"
                  disabled={!connectionId}
                  className="rounded-xl bg-violet-600 px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-violet-500 disabled:opacity-50"
                >
                  Search
                </button>

                {(searchQuery || searchInput) && (
                  <button
                    type="button"
                    onClick={clearSearch}
                    className={`rounded-xl border px-3 py-2.5 text-sm ${colors.input}`}
                  >
                    Clear
                  </button>
                )}

              </form>

            </div>

            {/* Messages */}
            {!connectionId ? (
              <div className="p-12 text-center">

                <div className="text-4xl">📩</div>

                <h3 className={`mt-4 font-semibold ${colors.heading}`}>
                  Select a Google account
                </h3>

                <p className={`mt-2 text-sm ${colors.muted}`}>
                  Choose a connected account to load your emails.
                </p>

              </div>
            ) : loading ? (
              <div className="space-y-3 p-5">

                {Array.from({ length: 6 }).map((_, index) => (
                  <div
                    key={index}
                    className={`h-16 animate-pulse rounded-xl ${colors.skeleton}`}
                  />
                ))}

              </div>
            ) : error && !messagesQuery.data ? (
              <div className="p-8 text-center">

                <p className="text-sm text-rose-500">
                  {error.message}
                </p>

                <button
                  type="button"
                  onClick={() => messagesQuery.refetch()}
                  className="mt-4 rounded-lg bg-violet-600 px-4 py-2 text-sm font-semibold text-white"
                >
                  Retry
                </button>

              </div>
            ) : (
              <>
                {error && (
                  <p role="status" className="px-5 pt-4 text-sm text-rose-500">
                    Unable to refresh this folder. Showing previously loaded emails.
                  </p>
                )}
                {messages.length === 0 ? (
                  <div className="p-12 text-center">

                    <div className="text-4xl">📭</div>

                    <h3 className={`mt-4 font-semibold ${colors.heading}`}>
                      No messages found
                    </h3>

                    <p className={`mt-2 text-sm ${colors.muted}`}>
                      There are no emails in this folder or matching your search.
                    </p>

                  </div>
                ) : (
                  <div
                    className={`gmail-scrollbar max-h-[650px] overflow-y-auto divide-y ${
                      isDark
                        ? 'divide-[#252838]'
                        : 'divide-slate-200'
                    }`}
                  >

                    {messages.map((message) => (
                      <div
                        key={message.id}
                        className={`flex w-full items-start gap-3 p-4 transition sm:gap-4 sm:p-5 ${colors.hover}`}
                      >

                        <button
                          type="button"
                          onClick={() => openMessageDetails(message.id)}
                          aria-label={`Open email: ${message.subject || 'No subject'}`}
                          className="flex min-w-0 flex-1 items-start gap-3 text-left sm:gap-4"
                        >

                          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-violet-500 to-indigo-600 text-sm font-bold text-white">
                            {getInitials(message.sender)}
                          </div>

                          <div className="min-w-0 flex-1">

                            <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">

                              <p className={`truncate text-sm font-semibold ${colors.heading}`}>
                                {message.sender || 'Unknown sender'}
                              </p>

                              <span className={`shrink-0 text-xs ${colors.muted}`}>
                                {formatDate(message.date)}
                              </span>

                            </div>

                            <p className={`mt-1 truncate text-sm font-medium ${colors.text}`}>
                              {message.subject || '(No subject)'}
                            </p>

                            <p className={`mt-1 line-clamp-2 text-xs leading-5 ${colors.muted}`}>
                              {message.snippet}
                            </p>

                            <div className="mt-2 flex flex-wrap gap-2">

                              {(message.labels || []).slice(0, 3).map((label) => (
                                <span
                                  key={label}
                                  className={`rounded-md px-2 py-1 text-[10px] ${colors.label}`}
                                >
                                  {label}
                                </span>
                              ))}

                            </div>

                          </div>

                        </button>

                        <div className="flex shrink-0 items-center gap-2">
                          {activeFolder === 'inbox' && (
                            <button
                              type="button"
                              aria-label={`Archive email: ${message.subject || 'No subject'}`}
                              title="Archive"
                              disabled={trashMessageMutation.isPending}
                              onClick={(event) =>
                                handleMessageAction(event, message, 'archive')
                              }
                              onKeyDown={(event) => event.stopPropagation()}
                              className={`group flex h-9 w-9 items-center justify-center rounded-xl border transition-all duration-200 hover:-translate-y-0.5 hover:shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-500/60 disabled:cursor-not-allowed disabled:opacity-50 ${
                                isDark
                                  ? 'border-violet-400/10 bg-violet-400/[0.06] text-violet-300/80 hover:border-violet-400/30 hover:bg-violet-400/15 hover:text-violet-200'
                                  : 'border-violet-100 bg-violet-50/80 text-violet-600 hover:border-violet-200 hover:bg-violet-100 hover:text-violet-700'
                              }`}
                            >
                              {trashMessageMutation.isPending &&
                              trashMessageMutation.variables?.messageId === message.id &&
                              trashMessageMutation.variables?.action === 'archive'
                                ? (
                                  <GmailActionSpinner />
                                )
                                : (
                                  <svg
                                    aria-hidden="true"
                                    viewBox="0 0 24 24"
                                    fill="none"
                                    stroke="currentColor"
                                    strokeLinecap="round"
                                    strokeLinejoin="round"
                                    strokeWidth="1.7"
                                    className="h-[17px] w-[17px] transition-transform duration-200 group-hover:scale-105"
                                  >
                                    <path d="M4 4h16l1 5H3l1-5Z" />
                                    <path d="M4 9v11h16V9M9 13h6" />
                                  </svg>
                                )}
                            </button>
                          )}

                          {activeFolder === 'trash' && (
                            <button
                              type="button"
                              aria-label={`Restore email: ${message.subject || 'No subject'}`}
                              title="Restore from Trash"
                              disabled={trashMessageMutation.isPending}
                              onClick={(event) =>
                                handleMessageAction(event, message, 'restore')
                              }
                              onKeyDown={(event) => event.stopPropagation()}
                              className={`group flex h-9 w-9 items-center justify-center rounded-xl border transition-all duration-200 hover:-translate-y-0.5 hover:shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/60 disabled:cursor-not-allowed disabled:opacity-50 ${
                                isDark
                                  ? 'border-emerald-400/10 bg-emerald-400/[0.06] text-emerald-300/80 hover:border-emerald-400/30 hover:bg-emerald-400/15 hover:text-emerald-200'
                                  : 'border-emerald-100 bg-emerald-50/80 text-emerald-600 hover:border-emerald-200 hover:bg-emerald-100 hover:text-emerald-700'
                              }`}
                            >
                              {trashMessageMutation.isPending &&
                              trashMessageMutation.variables?.messageId === message.id &&
                              trashMessageMutation.variables?.action === 'restore'
                                ? (
                                  <GmailActionSpinner />
                                )
                                : (
                                  <svg
                                    aria-hidden="true"
                                    viewBox="0 0 24 24"
                                    fill="none"
                                    stroke="currentColor"
                                    strokeLinecap="round"
                                    strokeLinejoin="round"
                                    strokeWidth="1.7"
                                    className="h-[17px] w-[17px] transition-transform duration-200 group-hover:scale-105"
                                  >
                                    <path d="M4 5h16l1 5H3l1-5Z" />
                                    <path d="M4 10v10h16V10" />
                                    <path d="M12 16V9m0 0-3 3m3-3 3 3" />
                                  </svg>
                                )}
                            </button>
                          )}

                          <button
                            type="button"
                            aria-label={`${
                              activeFolder === 'trash'
                                ? 'Delete permanently'
                                : 'Move email to Trash'
                            }: ${message.subject || 'No subject'}`}
                            title={
                              activeFolder === 'trash'
                                ? 'Delete permanently'
                                : 'Move to Trash'
                            }
                            disabled={trashMessageMutation.isPending}
                            onClick={(event) =>
                              handleMessageAction(
                                event,
                                message,
                                activeFolder === 'trash'
                                  ? 'permanent'
                                  : 'trash',
                              )
                            }
                            onKeyDown={(event) => event.stopPropagation()}
                            className={`group flex h-9 w-9 items-center justify-center rounded-xl border transition-all duration-200 hover:-translate-y-0.5 hover:shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-rose-500/60 disabled:cursor-not-allowed disabled:opacity-50 ${
                              isDark
                                ? 'border-rose-400/10 bg-rose-400/[0.06] text-rose-300/80 hover:border-rose-400/30 hover:bg-rose-400/15 hover:text-rose-200'
                                : 'border-rose-100 bg-rose-50/80 text-rose-500 hover:border-rose-200 hover:bg-rose-100 hover:text-rose-700'
                            }`}
                          >
                            {trashMessageMutation.isPending &&
                            trashMessageMutation.variables?.messageId === message.id &&
                            trashMessageMutation.variables?.action ===
                              (activeFolder === 'trash' ? 'permanent' : 'trash')
                              ? (
                                <GmailActionSpinner />
                              )
                              : (
                                <svg
                                  aria-hidden="true"
                                  viewBox="0 0 24 24"
                                  fill="none"
                                  stroke="currentColor"
                                  strokeLinecap="round"
                                  strokeLinejoin="round"
                                  strokeWidth="1.7"
                                  className="h-[17px] w-[17px] transition-transform duration-200 group-hover:scale-105"
                                >
                                  <path d="M4 7h16" />
                                  <path d="M10 11v6M14 11v6" />
                                  <path d="m6 7 1 13h10l1-13" />
                                  <path d="M9 7V4h6v3" />
                                </svg>
                              )}
                          </button>
                        </div>

                      </div>
                    ))}

                  </div>
                )}
              </>
            )}

            {/* Pagination */}
            {connectionId && messagesQuery.hasNextPage && (
              <div className={`border-t p-5 text-center ${colors.divider}`}>

                <button
                  type="button"
                  onClick={() => messagesQuery.fetchNextPage()}
                  disabled={messagesQuery.isFetchingNextPage}
                  className={`rounded-xl border px-5 py-2.5 text-sm font-semibold transition disabled:opacity-50 ${
                    isDark
                      ? 'border-[#393f56] text-slate-200 hover:bg-[#202333]'
                      : 'border-slate-200 text-slate-700 hover:bg-slate-100'
                  }`}
                >
                  {messagesQuery.isFetchingNextPage
                    ? 'Loading more...'
                    : 'Load more emails'}
                </button>

              </div>
            )}

            {messagesQuery.isFetchNextPageError && (
              <p className="p-4 text-center text-sm text-rose-500">
                Unable to load more emails. Please try again.
              </p>
            )}

          </div>

        </div>

      </section>

      {composeSuccess && (
        <div
          role="status"
          className="rounded-xl border border-emerald-500/30 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-500"
        >
          {composeSuccess}
        </div>
      )}

      {/* Compose Email Modal */}
      {showCompose && (
        <div
          className="fixed inset-0 z-[60] flex items-center justify-center bg-black/70 p-3 sm:p-6"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !sendEmailMutation.isPending) {
              setShowCompose(false)
            }
          }}
        >
          <section
            role="dialog"
            aria-modal="true"
            aria-labelledby="compose-email-title"
            className={`flex max-h-[92vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border shadow-2xl ${colors.card}`}
          >
            <div className={`flex items-center justify-between border-b px-5 py-4 ${colors.divider}`}>
              <div>
                <h2
                  id="compose-email-title"
                  className={`text-lg font-bold ${colors.heading}`}
                >
                  {composeMode === 'reply'
                    ? 'Reply'
                    : composeMode === 'reply-all'
                      ? 'Reply All'
                      : composeMode === 'forward'
                        ? 'Forward Email'
                        : 'New Message'}
                </h2>
                <p className={`mt-1 text-xs ${colors.muted}`}>
                  From: {selectedConnection?.email || 'Selected Google account'}
                </p>
              </div>
              <button
                type="button"
                onClick={() => setShowCompose(false)}
                disabled={sendEmailMutation.isPending}
                aria-label="Close compose window"
                className={`rounded-lg px-3 py-2 text-xl ${colors.hover} ${colors.text} disabled:opacity-50`}
              >
                ×
              </button>
            </div>
            
            <form onSubmit={handleSendEmail} className="gmail-scrollbar min-h-0 flex-1 overflow-y-auto">
              <div className="space-y-4 p-5">

                {(composeMode === 'compose' || composeMode === 'forward') && (
                  <>
                    <div>
                      <label
                        htmlFor="compose-to"
                        className={`mb-1 block text-xs font-semibold ${colors.muted}`}
                      >
                        To <span className="font-normal">(separate multiple addresses with commas)</span>
                      </label>

                      <input
                        id="compose-to"
                        type="text"
                        required
                        value={composeData.to}
                        onChange={(event) => handleComposeChange('to', event.target.value)}
                        placeholder="recipient@example.com"
                        className={`w-full rounded-lg border px-3 py-2.5 text-sm outline-none focus:border-violet-500 ${colors.input}`}
                      />
                    </div>

                    <div>
                      <label
                        htmlFor="compose-cc"
                        className={`mb-1 block text-xs font-semibold ${colors.muted}`}
                      >
                        CC
                      </label>

                      <input
                        id="compose-cc"
                        type="text"
                        value={composeData.cc}
                        onChange={(event) => handleComposeChange('cc', event.target.value)}
                        placeholder="Optional CC recipients"
                        className={`w-full rounded-lg border px-3 py-2.5 text-sm outline-none focus:border-violet-500 ${colors.input}`}
                      />
                    </div>

                    <div>
                      <label
                        htmlFor="compose-bcc"
                        className={`mb-1 block text-xs font-semibold ${colors.muted}`}
                      >
                        BCC
                      </label>

                      <input
                        id="compose-bcc"
                        type="text"
                        value={composeData.bcc}
                        onChange={(event) => handleComposeChange('bcc', event.target.value)}
                        placeholder="Optional BCC recipients"
                        className={`w-full rounded-lg border px-3 py-2.5 text-sm outline-none focus:border-violet-500 ${colors.input}`}
                      />
                    </div>
                  </>
                )}

                {composeMode === 'compose' && (
                  <div>
                    <label
                      htmlFor="compose-subject"
                      className={`mb-1 block text-xs font-semibold ${colors.muted}`}
                    >
                      Subject
                    </label>

                    <input
                      id="compose-subject"
                      type="text"
                      required
                      maxLength={998}
                      value={composeData.subject}
                      onChange={(event) => handleComposeChange('subject', event.target.value)}
                      placeholder="Email subject"
                      className={`w-full rounded-lg border px-3 py-2.5 text-sm outline-none focus:border-violet-500 ${colors.input}`}
                    />
                  </div>
                )}

                <div>
                  <label
                    htmlFor="compose-body"
                    className={`mb-1 block text-xs font-semibold ${colors.muted}`}
                  >
                    Message
                  </label>

                  <textarea
                    id="compose-body"
                    required={composeMode !== 'forward'}
                    rows={12}
                    maxLength={1000000}
                    value={composeData.body}
                    onChange={(event) => handleComposeChange('body', event.target.value)}
                    placeholder={
                      composeMode === 'reply'
                        ? 'Write your reply...'
                        : composeMode === 'reply-all'
                          ? 'Write your reply to everyone...'
                          : composeMode === 'forward'
                            ? 'Add a message before forwarding (optional)...'
                            : 'Write your email...'
                    }
                    className={`w-full resize-y rounded-lg border px-3 py-3 text-sm leading-6 outline-none focus:border-violet-500 ${colors.input}`}
                  />
                </div>

                {composeError && (
                  <p role="alert" className="text-sm text-rose-500">
                    {composeError}
                  </p>
                )}
              </div>

              <div className={`flex flex-wrap items-center justify-between gap-3 border-t p-4 ${colors.divider}`}>
                <p className={`text-xs ${colors.muted}`}>
                  You will be asked to confirm before sending.
                </p>

                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => setShowCompose(false)}
                    disabled={sendEmailMutation.isPending}
                    className={`rounded-lg border px-4 py-2.5 text-sm font-semibold ${colors.input} disabled:opacity-50`}
                  >
                    Cancel
                  </button>

                  <button
                    type="submit"
                    disabled={sendEmailMutation.isPending || !connectionId}
                    className="rounded-lg bg-violet-600 px-5 py-2.5 text-sm font-semibold text-white transition hover:bg-violet-500 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {sendEmailMutation.isPending
                      ? 'Sending...'
                      : composeMode === 'reply'
                        ? 'Send Reply'
                        : composeMode === 'reply-all'
                          ? 'Send Reply All'
                          : composeMode === 'forward'
                            ? 'Forward ✈'
                            : 'Send ✈'}
                  </button>
                </div>
              </div>
            </form>

          </section>
        </div>
      )}

      {messageActionConfirmation && (
        <div
          className="fixed inset-0 z-[70] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) {
              closeMessageActionConfirmation()
            }
          }}
        >
          <section
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="trash-confirmation-title"
            aria-describedby="trash-confirmation-description"
            className={`w-full max-w-md rounded-2xl border p-6 shadow-2xl ${
              isDark
                ? 'border-[#303448] bg-[#11131c]'
                : 'border-slate-200 bg-white'
            }`}
          >
            <div className="flex items-start justify-between gap-4">
              <div className="flex items-center gap-3">
                <div
                  className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-xl ${
                    isDark
                      ? 'bg-rose-500/15 text-rose-400'
                      : 'bg-rose-50 text-rose-600'
                  }`}
                  aria-hidden="true"
                >
                  <svg
                    xmlns="http://www.w3.org/2000/svg"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    className="h-5 w-5"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      d="M3 6h18m-2 0-.9 13.1A2 2 0 0 1 16.1 21H7.9a2 2 0 0 1-2-1.9L5 6m4 0V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2m-5 4v7m4-7v7"
                    />
                  </svg>
                </div>
                <div>
                  <h2
                    id="trash-confirmation-title"
                    className={`text-lg font-semibold ${colors.heading}`}
                  >
                    {{
                      permanent: 'Delete permanently?',
                      restore: 'Restore from Trash?',
                      archive: 'Archive email?',
                      trash: 'Move to Trash?',
                    }[messageActionConfirmation.action]}
                  </h2>
                  {messageActionConfirmation.subject && (
                    <p className={`mt-1 truncate text-sm ${colors.muted}`}>
                      {messageActionConfirmation.subject}
                    </p>
                  )}
                </div>
              </div>
              <button
                type="button"
                aria-label="Close confirmation"
                onClick={closeMessageActionConfirmation}
                disabled={trashMessageMutation.isPending}
                className={`-mr-2 -mt-2 rounded-lg p-2 text-xl transition disabled:cursor-not-allowed disabled:opacity-50 ${colors.muted} ${colors.hover}`}
              >
                ×
              </button>
            </div>

            <p
              id="trash-confirmation-description"
              className={`mt-5 text-sm leading-6 ${colors.text}`}
            >
              {{
                permanent:
                  'This email will be permanently deleted and cannot be recovered. Are you sure you want to continue?',
                restore:
                  'Restore this email from Trash? It will return to its mailbox.',
                archive:
                  'Archive this email? It will be removed from Inbox and remain available in All Mail.',
                trash:
                  'Are you sure you want to move this email to Trash? You can restore it later from the Trash folder.',
              }[messageActionConfirmation.action]}
            </p>

            {trashNotice?.type === 'error' && (
              <div className="mt-4">
                <p role="alert" className="text-sm text-rose-500">
                  {trashNotice.message}
                </p>
                {messageActionConfirmation.action === 'permanent' &&
                  trashNotice.message.includes('additional Google permission') && (
                    <button
                      type="button"
                      onClick={() => reconnectGoogleMutation.mutate()}
                      disabled={reconnectGoogleMutation.isPending}
                      className="mt-3 rounded-lg border border-violet-500/30 px-3 py-2 text-sm font-semibold text-violet-500 transition hover:bg-violet-500/10 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {reconnectGoogleMutation.isPending
                        ? 'Reconnecting...'
                        : 'Reconnect Google account'}
                    </button>
                  )}
              </div>
            )}

            <div className="mt-7 flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
              <button
                type="button"
                onClick={closeMessageActionConfirmation}
                disabled={trashMessageMutation.isPending}
                className={`rounded-xl border px-4 py-2.5 text-sm font-semibold transition disabled:cursor-not-allowed disabled:opacity-50 ${colors.input}`}
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={confirmMessageAction}
                disabled={trashMessageMutation.isPending}
                className={`rounded-xl px-4 py-2.5 text-sm font-semibold text-white transition disabled:cursor-not-allowed disabled:opacity-60 ${
                  messageActionConfirmation.action === 'archive'
                    ? 'bg-violet-600 hover:bg-violet-500'
                    : messageActionConfirmation.action === 'restore'
                      ? 'bg-emerald-600 hover:bg-emerald-500'
                      : 'bg-rose-600 hover:bg-rose-500'
                }`}
              >
                {trashMessageMutation.isPending
                  ? {
                      permanent: 'Deleting...',
                      restore: 'Restoring...',
                      archive: 'Archiving...',
                      trash: 'Moving...',
                    }[messageActionConfirmation.action]
                  : {
                      permanent: 'Delete permanently',
                      restore: 'Restore',
                      archive: 'Archive',
                      trash: 'Move to Trash',
                    }[messageActionConfirmation.action]}
              </button>
            </div>
          </section>
        </div>
      )}

      {/* Email Details Modal */}
      {selectedMessageId && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-3 sm:p-6"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) {
              closeMessageDetails()
            }
          }}
        >

          <section
            role="dialog"
            aria-modal="true"
            aria-labelledby="email-detail-title"
            className={`flex max-h-[92vh] w-full max-w-5xl flex-col overflow-hidden rounded-2xl border shadow-2xl ${colors.card}`}
          >

            {/* Modal header */}
            <div className={`flex shrink-0 items-center justify-between border-b p-5 ${colors.divider}`}>

              <div>
                <h2
                  id="email-detail-title"
                  className={`text-lg font-bold ${colors.heading}`}
                >
                  Email Details
                </h2>

                <p className={`mt-1 text-xs ${colors.muted}`}>
                  Full email information
                </p>
              </div>

              <button
                type="button"
                onClick={closeMessageDetails}
                aria-label="Close email details"
                className={`rounded-lg px-3 py-2 text-xl ${colors.hover} ${colors.text}`}
              >
                ×
              </button>

            </div>

            {/* Scrollable email content */}
            <div className="gmail-scrollbar min-h-0 flex-1 overflow-y-auto overscroll-contain p-5">

              {messageDetailQuery.isLoading ? (
                <div className="space-y-4">
                  <div className={`h-6 w-3/4 animate-pulse rounded ${colors.skeleton}`} />
                  <div className={`h-4 w-1/2 animate-pulse rounded ${colors.skeleton}`} />
                  <div className={`h-40 animate-pulse rounded ${colors.skeleton}`} />
                </div>
              ) : messageDetailQuery.isError ? (
                <div className="py-10 text-center">

                  <p className="text-sm text-rose-500">
                    {messageDetailQuery.error.message}
                  </p>

                  <button
                    type="button"
                    onClick={() => messageDetailQuery.refetch()}
                    className="mt-4 rounded-lg bg-violet-600 px-4 py-2 text-sm font-semibold text-white"
                  >
                    Retry
                  </button>

                </div>
              ) : selectedMessage ? (
                <div className="space-y-5">

                  {/* Subject */}
                  <h3 className={`break-words text-xl font-bold ${colors.heading}`}>
                    {selectedMessage.headers?.subject || '(No subject)'}
                  </h3>

                  {/* Email metadata */}
                  <div className={`space-y-2 text-sm ${colors.text}`}>

                    <p className="break-words">
                      <span className={colors.muted}>From: </span>
                      {selectedMessage.headers?.from || 'Unknown sender'}
                    </p>

                    <p className="break-words">
                      <span className={colors.muted}>To: </span>
                      {selectedMessage.headers?.to || 'Unknown recipient'}
                    </p>

                    {selectedMessage.headers?.cc && (
                      <p className="break-words">
                        <span className={colors.muted}>CC: </span>
                        {selectedMessage.headers.cc}
                      </p>
                    )}

                    {selectedMessage.headers?.bcc && (
                      <p className="break-words">
                        <span className={colors.muted}>BCC: </span>
                        {selectedMessage.headers.bcc}
                      </p>
                    )}

                    {selectedMessage.headers?.['reply-to'] && (
                      <p className="break-words">
                        <span className={colors.muted}>Reply-To: </span>
                        {selectedMessage.headers['reply-to']}
                      </p>
                    )}

                    <p>
                      <span className={colors.muted}>Date: </span>
                      {formatFullDate(
                        selectedMessage.headers?.date ||
                        selectedMessage.internal_date,
                      )}
                    </p>

                  </div>

                  <div className={`border-t ${colors.divider}`} />

                  {/* Body format selector */}
                  {hasHtml && hasPlainText && (
                    <div className="flex flex-wrap gap-2">

                      <button
                        type="button"
                        onClick={() => setShowHtmlBody(false)}
                        className={`rounded-lg px-3 py-2 text-xs font-semibold ${
                          !showHtmlBody
                            ? 'bg-violet-600 text-white'
                            : colors.label
                        }`}
                      >
                        Plain text
                      </button>

                      <button
                        type="button"
                        onClick={() => setShowHtmlBody(true)}
                        className={`rounded-lg px-3 py-2 text-xs font-semibold ${
                          showHtmlBody
                            ? 'bg-violet-600 text-white'
                            : colors.label
                        }`}
                      >
                        HTML view
                      </button>

                    </div>
                  )}

                  {/* Email body */}
                  <div
                    className={`overflow-hidden rounded-xl border ${
                      isDark
                        ? 'border-[#252838] bg-[#171a27]'
                        : 'border-slate-200 bg-slate-50'
                    }`}
                  >

                    {showHtmlBody && hasHtml ? (
                      <iframe
                        title="Email HTML content"
                        sandbox=""
                        referrerPolicy="no-referrer"
                        srcDoc={safeHtmlDocument}
                        className="h-[65vh] min-h-[350px] w-full border-0 bg-white"
                      />
                    ) : (
                      <div className={`gmail-scrollbar max-h-[65vh] overflow-y-auto whitespace-pre-wrap break-words p-5 text-sm leading-7 ${colors.text}`}>
                        {emailBody.plain_text ||
                          emailBody.text ||
                          selectedMessage.snippet ||
                          'No email body available.'}
                      </div>
                    )}

                  </div>

                  {/* Attachments */}
                  {attachments.length > 0 && (
                    <div>

                      <h4 className={`mb-3 font-semibold ${colors.heading}`}>
                        Attachments ({attachments.length})
                      </h4>

                      {attachmentDownloadError && (
                        <p className="mb-3 text-sm text-rose-500">
                          {attachmentDownloadError}
                        </p>
                      )}

                      <div className="space-y-2">

                        {attachments.map((attachment, index) => (
                          <div
                            key={
                              attachment.attachment_id ||
                              attachment.attachmentId ||
                              index
                            }
                            className={`rounded-lg border p-3 ${colors.card}`}
                          >

                            <div className="flex items-start gap-3">

                              <span className="text-xl">📎</span>

                              <div className="min-w-0 flex-1">

                                <p className={`break-words text-sm font-medium ${colors.text}`}>
                                  {getAttachmentName(attachment)}
                                </p>

                                <p className={`mt-1 text-xs ${colors.muted}`}>
                                  {attachment.mime_type ||
                                    attachment.mimeType ||
                                    'Unknown file type'}

                                  {attachment.size
                                    ? ` • ${Number(attachment.size).toLocaleString()} bytes`
                                    : ''}
                                </p>

                                {attachment.is_inline && (
                                  <span className={`mt-1 inline-block rounded px-2 py-1 text-[10px] ${colors.label}`}>
                                    Inline attachment
                                  </span>
                                )}

                              </div>

                              <button
                                type="button"
                                onClick={() => handleAttachmentDownload(attachment)}
                                disabled={
                                  !attachment.attachment_id ||
                                  downloadingAttachmentId === attachment.attachment_id
                                }
                                className="shrink-0 rounded-lg bg-violet-600 px-3 py-2 text-xs font-semibold text-white transition hover:bg-violet-500 disabled:cursor-not-allowed disabled:opacity-50"
                              >
                                {downloadingAttachmentId === attachment.attachment_id
                                  ? 'Downloading...'
                                  : '↓ Download'}
                              </button>

                            </div>

                          </div>
                        ))}

                      </div>

                    </div>
                  )}

                </div>
              ) : (
                <p className={`py-10 text-center text-sm ${colors.muted}`}>
                  Email details are not available.
                </p>
              )}

            </div>
                        
            {/* Modal footer */}
            <div className={`flex shrink-0 flex-wrap items-center justify-between gap-3 border-t p-4 ${colors.divider}`}
            >
              <div className="flex flex-wrap items-center gap-2">

                {selectedMessage && !messageDetailQuery.isLoading && (
                  <>
                    <button
                      type="button"
                      onClick={() => openCompose('reply')}
                      className={`rounded-xl border px-4 py-2.5 text-sm font-semibold transition ${colors.input} ${colors.hover}`}
                    >
                      ↩ Reply
                    </button>

                    <button
                      type="button"
                      onClick={() => openCompose('reply-all')}
                      className={`rounded-xl border px-4 py-2.5 text-sm font-semibold transition ${colors.input} ${colors.hover}`}
                    >
                      ↩ Reply All
                    </button>

                    <button
                      type="button"
                      onClick={() => openCompose('forward')}
                      className={`rounded-xl border px-4 py-2.5 text-sm font-semibold transition ${colors.input} ${colors.hover}`}
                    >
                      ➦ Forward
                    </button>
                  </>
                )}

              </div>

              <button
                type="button"
                onClick={closeMessageDetails}
                className="rounded-xl bg-violet-600 px-5 py-2.5 text-sm font-semibold text-white hover:bg-violet-500"
              >
                Back to Inbox
              </button>
            </div>
          </section>

        </div>
      )}

    </div>
  )
}
