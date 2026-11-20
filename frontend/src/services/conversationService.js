
import { api } from '../lib/api.js'
import { supabase } from '../lib/supabase.js'

const BASE_PATH = '/api/v1/conversations'
const CHAT_PATH = '/api/v1/chat'
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL

export const conversationService = {
  // Get all conversations belonging to the authenticated user.
  async getConversations() {
    return api.get(BASE_PATH)
  },

  // Get one conversation by ID.
  async getConversation(conversationId) {
    return api.get(
      `${BASE_PATH}/${encodeURIComponent(conversationId)}`
    )
  },

  // Create a new conversation.
  async createConversation(title = 'New conversation') {
    return api.post(BASE_PATH, { title })
  },

  // Update conversation title.
  async updateConversation(conversationId, title) {
    return api.patch(
      `${BASE_PATH}/${encodeURIComponent(conversationId)}`,
      { title }
    )
  },

  // Archive a conversation.
  async archiveConversation(conversationId) {
    return api.patch(
      `${BASE_PATH}/${encodeURIComponent(conversationId)}/archive`
    )
  },

  // Permanently delete a conversation.
  async deleteConversation(conversationId) {
    return api.delete(
      `${BASE_PATH}/${encodeURIComponent(conversationId)}`
    )
  },

  // Get messages belonging to a conversation.
  async getMessages(conversationId, limit = 100) {
    const params = new URLSearchParams({
      limit: String(limit),
    })

    return api.get(
      `${BASE_PATH}/${encodeURIComponent(conversationId)}/messages?${params.toString()}`
    )
  },

  async updateEmailDraft(conversationId, messageId, emailDraft) {
    return api.patch(
      `${BASE_PATH}/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(messageId)}/draft`,
      { email_draft: emailDraft },
    )
  },

  async saveEmailDraftToGmail(
    conversationId,
    messageId,
    connectionId,
    operationId,
  ) {
    return api.post(
      `${BASE_PATH}/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(messageId)}/draft/save`,
      {
        connection_id: connectionId,
        operation_id: operationId,
      },
    )
  },

  async getEmailDraftSaveStatus(
    conversationId,
    messageId,
    connectionId,
    operationId,
  ) {
    const params = new URLSearchParams({
      connection_id: connectionId,
      operation_id: operationId,
    })

    return api.get(
      `${BASE_PATH}/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(messageId)}/draft/save-status?${params.toString()}`
    )
  },

  // Create a user message directly.
  // Use this only when a standalone message insert is required.
  async createMessage(conversationId, content) {
    return api.post(
      `${BASE_PATH}/${encodeURIComponent(conversationId)}/messages`,
      { content }
    )
  },

  // Send a message through the LangGraph chat workflow (non-streaming).
  async sendChatMessage(
    conversationId,
    content,
    idempotencyKey,
    gmailConnectionId = null,
    userTimezone = 'UTC',
  ) {
    return api.post(
      CHAT_PATH,
      {
        conversation_id: conversationId,
        content,
        gmail_connection_id: gmailConnectionId,
        user_timezone: userTimezone,
      },
      {
        'Idempotency-Key': idempotencyKey,
      }
    )
  },

  // Stream a chat message via SSE (POST). Calls onEvent({type, ...}) for each SSE event.
  async streamChatMessage(
    conversationId,
    content,
    idempotencyKey,
    gmailConnectionId = null,
    userTimezone = 'UTC',
    onEvent = () => {},
    signal,
  ) {
    const {
      data: { session },
      error,
    } = await supabase.auth.getSession()
    if (error || !session?.access_token) {
      throw new Error('Authentication required. Please sign in again.')
    }
    const url = `${API_BASE_URL}${CHAT_PATH}/stream`
    const resp = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${session.access_token}`,
        'Idempotency-Key': idempotencyKey,
        Accept: 'text/event-stream',
      },
      body: JSON.stringify({
        conversation_id: conversationId,
        content,
        gmail_connection_id: gmailConnectionId,
        user_timezone: userTimezone,
      }),
      signal,
    })
    if (!resp.ok) {
      let detail = 'Request failed'
      try {
        const errBody = await resp.json()
        detail = errBody?.detail || detail
      } catch (_) {
        // ignore
      }
      const err = new Error(typeof detail === 'string' ? detail : 'Request failed')
      err.status = resp.status
      throw err
    }
    const reader = resp.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''
    let sawDone = false
    let sawMalformed = false

    const dispatch = (evt) => {
      if (sawMalformed) return
      if (!evt || typeof evt !== 'object' || !evt.type) return
      if (evt.type === 'error') {
        const err = new Error(evt.message || 'Streaming failed')
        err.partial = Boolean(evt.partial)
        err.streamError = true
        throw err
      }
      if (evt.type === 'done') sawDone = true
      onEvent(evt)
    }

    const handleLine = (line) => {
      if (!line.startsWith('data:')) return
      const jsonStr = line.slice(5).trimStart()
      if (!jsonStr) return
      let evt
      try {
        evt = JSON.parse(jsonStr)
      } catch {
        // A malformed frame must never be presented as a complete answer.
        // Suppress later events (including `done`) and fail at stream end.
        sawMalformed = true
        return
      }
      dispatch(evt)
    }

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      // Parse SSE events separated by \n\n
      let idx
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const rawEvent = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)
        for (const line of rawEvent.split('\n')) {
          handleLine(line)
        }
      }
    }
    // Flush any remaining buffer
    if (buffer.trim()) {
      for (const line of buffer.split('\n')) {
        handleLine(line)
      }
    }

    if (sawMalformed) {
      const err = new Error('Some streamed content could not be parsed.')
      err.partial = true
      throw err
    }
    if (!sawDone) {
      const err = new Error('The response stream ended before completion.')
      err.partial = true
      throw err
    }
  },

  async getChatRequestStatus(conversationId, idempotencyKey) {
    const params = new URLSearchParams({
      conversation_id: conversationId,
    })
    return api.get(
      `${CHAT_PATH}/requests/${encodeURIComponent(idempotencyKey)}?${params.toString()}`
    )
  },
}
