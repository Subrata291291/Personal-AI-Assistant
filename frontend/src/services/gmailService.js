
import { api } from '../lib/api.js'

const BASE_PATH = '/api/v1/gmail'

export const gmailService = {
  async getMessages({
    connectionId,
    maxResults = 20,
    pageToken,
    query,
    folder = 'inbox',
  }) {
    if (!connectionId) {
      throw new Error('Google connection is required.')
    }

    const params = new URLSearchParams({
      connection_id: connectionId,
      max_results: String(maxResults),
      folder,
    })

    if (pageToken) params.set('page_token', pageToken)
    if (query?.trim()) params.set('query', query.trim())

    return api.get(`${BASE_PATH}/messages?${params.toString()}`)
  },

  async getMessage({ connectionId, messageId }) {
    if (!connectionId || !messageId) {
      throw new Error('Connection ID and message ID are required.')
    }

    const params = new URLSearchParams({
      connection_id: connectionId,
    })

    return api.get(
      `${BASE_PATH}/messages/${encodeURIComponent(messageId)}?${params.toString()}`
    )
  },

  async trashMessage({ connectionId, messageId }) {
    if (!connectionId || !messageId) {
      throw new Error('Connection ID and message ID are required.')
    }

    return api.post(
      `${BASE_PATH}/messages/${encodeURIComponent(messageId)}/trash`,
      { connection_id: connectionId },
    )
  },

  async restoreMessage({ connectionId, messageId }) {
    if (!connectionId || !messageId) {
      throw new Error('Connection ID and message ID are required.')
    }

    return api.post(
      `${BASE_PATH}/messages/${encodeURIComponent(messageId)}/untrash`,
      { connection_id: connectionId },
    )
  },

  async archiveMessage({ connectionId, messageId }) {
    if (!connectionId || !messageId) {
      throw new Error('Connection ID and message ID are required.')
    }

    return api.post(
      `${BASE_PATH}/messages/${encodeURIComponent(messageId)}/archive`,
      { connection_id: connectionId },
    )
  },

  async permanentlyDeleteMessage({ connectionId, messageId }) {
    if (!connectionId || !messageId) {
      throw new Error('Connection ID and message ID are required.')
    }

    return api.post(
      `${BASE_PATH}/messages/${encodeURIComponent(messageId)}/delete-permanently`,
      { connection_id: connectionId },
    )
  },

  async downloadAttachment({
    connectionId,
    messageId,
    attachmentId,
  }) {
    if (!connectionId || !messageId || !attachmentId) {
      throw new Error(
        'Connection ID, message ID and attachment ID are required.'
      )
    }

    const params = new URLSearchParams({
      connection_id: connectionId,
    })

    const path =
      `${BASE_PATH}/messages/${encodeURIComponent(messageId)}` +
      `/attachments/${encodeURIComponent(attachmentId)}` +
      `?${params.toString()}`

    return api.getBlob(path)
  },

  async sendMessage({
    connectionId,
    to,
    cc = [],
    bcc = [],
    subject,
    body,
    htmlBody = null,
    replyTo = null,
  }) {
    if (!connectionId) {
      throw new Error('Google connection is required.')
    }

    if (!Array.isArray(to) || to.length === 0) {
      throw new Error('At least one recipient is required.')
    }

    if (!subject?.trim()) {
      throw new Error('Email subject is required.')
    }

    if (!body?.trim() && !htmlBody?.trim()) {
      throw new Error('Email body is required.')
    }

    return api.post(`${BASE_PATH}/messages/send`, {
      connection_id: connectionId,
      to,
      cc,
      bcc,
      subject,
      body,
      html_body: htmlBody,
      reply_to: replyTo,
    })
  },

  async replyMessage({
    connectionId,
    messageId,
    body,
    htmlBody = null,
  }) {
    if (!connectionId || !messageId) {
      throw new Error('Connection ID and message ID are required.')
    }

    if (!body?.trim() && !htmlBody?.trim()) {
      throw new Error('Reply body is required.')
    }

    return api.post(
      `${BASE_PATH}/messages/${encodeURIComponent(messageId)}/reply`,
      {
        connection_id: connectionId,
        body,
        html_body: htmlBody,
      }
    )
  },

  async replyAllMessage({
    connectionId,
    messageId,
    body,
    htmlBody = null,
  }) {
    if (!connectionId || !messageId) {
      throw new Error('Connection ID and message ID are required.')
    }

    if (!body?.trim() && !htmlBody?.trim()) {
      throw new Error('Reply body is required.')
    }

    return api.post(
      `${BASE_PATH}/messages/${encodeURIComponent(messageId)}/reply-all`,
      {
        connection_id: connectionId,
        body,
        html_body: htmlBody,
      }
    )
  },

  async forwardMessage({
    connectionId,
    messageId,
    to,
    cc = [],
    bcc = [],
    body = '',
    htmlBody = null,
  }) {
    if (!connectionId || !messageId) {
      throw new Error('Connection ID and message ID are required.')
    }

    if (!Array.isArray(to) || to.length === 0) {
      throw new Error('At least one recipient is required.')
    }

    return api.post(
      `${BASE_PATH}/messages/${encodeURIComponent(messageId)}/forward`,
      {
        connection_id: connectionId,
        to,
        cc,
        bcc,
        body,
        html_body: htmlBody,
      }
    )
  },

  async getConnections() {
    const result = await api.get('/api/v1/oauth/connections')
    return result.connections || []
  },
}
