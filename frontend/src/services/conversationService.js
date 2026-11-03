
import { api } from '../lib/api.js'

const BASE_PATH = '/api/v1/conversations'
const CHAT_PATH = '/api/v1/chat'

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

  // Create a user message directly.
  // Use this only when a standalone message insert is required.
  async createMessage(conversationId, content) {
    return api.post(
      `${BASE_PATH}/${encodeURIComponent(conversationId)}/messages`,
      { content }
    )
  },

  // Send a message through the LangGraph chat workflow.
  async sendChatMessage(
    conversationId,
    content,
    idempotencyKey,
    gmailConnectionId = null,
  ) {
    return api.post(
      CHAT_PATH,
      {
        conversation_id: conversationId,
        content,
        gmail_connection_id: gmailConnectionId,
      },
      {
        'Idempotency-Key': idempotencyKey,
      }
    )
  },
}
