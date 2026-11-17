import { api } from '../lib/api.js'

const BASE_PATH = '/api/v1/contacts'

export const contactsService = {
  async resolveContext() {
    return api.get(`${BASE_PATH}/resolve`)
  },
  async listContacts({ pageSize = 25, pageToken, query, signal } = {}) {
    const params = new URLSearchParams({ page_size: String(pageSize) })
    if (pageToken) params.set('page_token', pageToken)
    if (query?.trim()) params.set('query', query.trim())
    const path = `${BASE_PATH}?${params.toString()}`
    return signal ? api.get(path, { signal }) : api.get(path)
  },
  async getContact(resourceName) {
    if (!resourceName) throw new Error('A contact is required.')
    return api.get(`${BASE_PATH}/${encodeURIComponent(resourceName)}`)
  },
  async createProposal(payload, options = {}) {
    const headers = {}
    if (options.idempotencyKey) {
      headers['Idempotency-Key'] = options.idempotencyKey
    }
    return api.post(`${BASE_PATH}/proposals`, payload, Object.keys(headers).length ? headers : undefined)
  },
}