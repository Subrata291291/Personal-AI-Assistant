import { api } from '../lib/api.js'

const BASE_PATH = '/api/v1/action-approvals'

export const actionApprovalService = {
  createProposal(payload) {
    return api.post(BASE_PATH, payload)
  },

  getPending(conversationId) {
    const params = new URLSearchParams()
    if (conversationId) {
      params.set('conversation_id', conversationId)
    }
    const query = params.toString()
    return api.get(query ? `${BASE_PATH}?${query}` : BASE_PATH)
  },

  getProposal(proposalId) {
    return api.get(`${BASE_PATH}/${encodeURIComponent(proposalId)}`)
  },

  approve(proposalId) {
    return api.post(
      `${BASE_PATH}/${encodeURIComponent(proposalId)}/approve`,
      null,
    )
  },

  reject(proposalId) {
    return api.post(
      `${BASE_PATH}/${encodeURIComponent(proposalId)}/reject`,
      null,
    )
  },

  execute(proposalId) {
    return api.post(
      `${BASE_PATH}/${encodeURIComponent(proposalId)}/execute`,
      null,
    )
  },
}
